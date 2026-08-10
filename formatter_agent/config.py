"""Environment configuration for the formatter A2A service.

Self-contained for the same reason as coordinator/config.py: this folder is
built and deployed on its own (`gcloud run deploy --source ./formatter_agent`,
`gcloud builds submit ./formatter_agent`), so it cannot import from the repo
root. See that file's header for why the duplication is deliberate.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# `uvicorn formatter_agent.agent:a2a_app` does NOT auto-load a package .env the
# way `adk web` does, so load it explicitly before the first model call.
# Real environment variables still win (override=False), which is how Cloud Run
# and Kubernetes inject config without shipping a file.
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# --- Google Cloud -----------------------------------------------------------
PROJECT_ID = _env("GOOGLE_CLOUD_PROJECT")
LOCATION = _env("GOOGLE_CLOUD_LOCATION", "us-central1")

# --- Model ------------------------------------------------------------------
# Must match the coordinator's model family. "gemini-flash-latest" is an AI
# Studio alias that 404s on Vertex — keep this a real publisher model id.
MODEL = _env("ADK_MODEL", "gemini-2.5-flash")

# --- Thinking ---------------------------------------------------------------
# This service is the ANALYST. Its reasoning is what turns gathered figures into
# a comparison with a conclusion, so it is never disabled for speed — the web
# app's Deep research toggle covers the router and the specialists only.
#
#    0  disables thinking     -1  automatic (the model's own default)
THINKING_ANALYST = int(_env("THINKING_ANALYST", "-1"))


def thinking_config(budget: int):
    """A GenerateContentConfig carrying a thinking budget.

    Duplicated from coordinator/config.py for the same reason the analyst prompt
    is: this package is built and deployed from its own folder and cannot import
    from the repo root.
    """
    from google.genai import types

    if budget is None:
        return None
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_budget=budget)
    )


# --- A2A advertised RPC location --------------------------------------------
# These do NOT bind the server — uvicorn's --host/--port do that. They are baked
# into the auto-generated agent card as rpc_url = "{protocol}://{host}:{port}/",
# and the coordinator dials whatever the card says. If they disagree with the
# real bind address, the card advertises somewhere unreachable.
#
#   local:      localhost / 8001 / http     (the defaults, what ./run.sh uses)
#   Cloud Run:  <svc>-<projnum>.<region>.run.app / 443 / https
#   Kubernetes: formatter-agent / 8080 / http   (in-cluster Service DNS)
A2A_HOST = _env("A2A_HOST", "localhost")
A2A_PORT = int(_env("A2A_PORT", "8001"))
A2A_PROTOCOL = _env("A2A_PROTOCOL", "http")
