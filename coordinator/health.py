"""Startup capability probes.

These are pure functions with no side effects, called by whoever is starting the
system — never at import time. Import-time network I/O would make `adk deploy`
cold starts flaky and would hang `adk web` behind a timeout on a bad connection,
for no benefit.

The results decide how the agent graph is built, which is why they run before
`build_root_agent` rather than inside it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

DISCOVERY_ENGINE_ROOT = "https://discoveryengine.googleapis.com/v1"


@dataclass(frozen=True)
class FormatterStatus:
    reachable: bool
    url: str
    advertised_url: str = ""
    name: str = ""
    error: str = ""


def probe_formatter(url: str, timeout: float = 3.0) -> FormatterStatus:
    """Fetch the remote formatter's A2A agent card.

    WHY THIS MATTERS: RemoteA2aAgent resolves its card LAZILY, inside the turn.
    An unreachable formatter therefore does not fail at startup — it fails after
    the specialists have already run, so the user watches a half-finished answer
    die at the last step. Probing up front turns that into a clean, visible
    fallback to an in-process formatter.

    A card that resolves but advertises a different host than we dialled is
    reported too: that is the classic "deployed but still advertising localhost"
    misconfiguration, where the fetch succeeds and the RPC call goes nowhere.
    """
    if not url:
        return FormatterStatus(reachable=False, url=url, error="No formatter URL configured.")

    try:
        response = httpx.get(url, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
        card = response.json()
    except Exception as exc:
        return FormatterStatus(
            reachable=False, url=url, error=f"{type(exc).__name__}: {exc}"
        )

    if not isinstance(card, dict) or "url" not in card:
        return FormatterStatus(
            reachable=False, url=url, error="Response is not a valid A2A agent card."
        )

    return FormatterStatus(
        reachable=True,
        url=url,
        advertised_url=str(card.get("url", "")),
        name=str(card.get("name", "")),
    )


def probe_datastore(datastore_path: str, project_id: str, timeout: float = 5.0) -> bool:
    """Check that the Vertex AI Search datastore exists and we can read it.

    Uses the Discovery Engine REST API with ADC rather than adding the
    google-cloud-discoveryengine dependency — google.auth already ships with
    google-cloud-aiplatform, which this package requires anyway.

    The `x-goog-user-project` header is load-bearing. Without it, ADC bills the
    request to a shared consumer project and the call comes back
    403 SERVICE_DISABLED even when the datastore is perfectly healthy — an
    inconclusive failure that looks exactly like a missing datastore.
    """
    if not datastore_path or not project_id:
        return False

    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(google.auth.transport.requests.Request())

        response = httpx.get(
            f"{DISCOVERY_ENGINE_ROOT}/{datastore_path}",
            headers={
                "Authorization": f"Bearer {credentials.token}",
                "x-goog-user-project": project_id,
            },
            timeout=timeout,
        )
        return response.status_code == 200
    except Exception:
        # Any failure — no ADC, no network, API disabled, permission denied —
        # means the lane cannot be trusted. Degrade rather than raise.
        return False
