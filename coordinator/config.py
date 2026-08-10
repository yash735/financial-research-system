"""Environment configuration for the coordinator package.

WHY THIS FILE DUPLICATES formatter_agent/config.py AND webapp/config.py
----------------------------------------------------------------------
`./coordinator` is deployed on its own — `adk deploy agent_engine ./coordinator`,
`gcloud builds submit ./coordinator`, and the GKE image all ship this folder and
nothing else. So it cannot import a shared module from the repo root. The three
config shims are near-identical `os.environ.get` blocks on purpose; that is the
price of keeping each deployable unit self-contained, and it is cheaper than the
alternatives (a published internal package, or a build step that copies files).

Nothing here has a default that points at a real cloud resource. Copy
`.env.example` to `.env` and fill it in — see docs/local-development.md.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load this package's .env if present, then the repo-root .env as a fallback.
# `override=False` (the default) means a real environment variable always wins,
# which is what lets Cloud Run / GKE / Agent Engine inject config without a file.
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    """Environment lookup that treats whitespace-only values as unset."""
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


# --- Google Cloud -----------------------------------------------------------
PROJECT_ID = _env("GOOGLE_CLOUD_PROJECT")
LOCATION = _env("GOOGLE_CLOUD_LOCATION", "us-central1")

# --- Model ------------------------------------------------------------------
# Pinned to a Vertex AI *publisher* model id. Do NOT use "gemini-flash-latest":
# that is an AI Studio / Gemini-API alias and it 404s on Vertex, which is where
# this must run because VertexAiSearchTool is Vertex-only.
MODEL = _env("ADK_MODEL", "gemini-2.5-flash")

# --- Remote formatter (A2A) -------------------------------------------------
# Defaults to the local uvicorn service that ./run.sh starts. Point it at a
# Cloud Run URL or an in-cluster Service to move the formatter off-box:
#   https://<service>-<projectnum>.<region>.run.app/.well-known/agent-card.json
#   http://formatter-agent:8080/.well-known/agent-card.json
FORMATTER_A2A_URL = _env(
    "FORMATTER_A2A_URL",
    "http://localhost:8001/.well-known/agent-card.json",
)

# --- Vertex AI Search datastore (optional lane) -----------------------------
# Accepts either a bare datastore id (e.g. "annual-reports") or a full resource
# path. A bare id is expanded using PROJECT_ID and VERTEX_SEARCH_LOCATION.
VERTEX_SEARCH_LOCATION = _env("VERTEX_SEARCH_LOCATION", "global")
_DATASTORE_RAW = _env("VERTEX_SEARCH_DATASTORE")

# "auto" probes the datastore at startup and disables the lane if unreachable.
# "on" trusts it blindly, "off" skips the probe and disables the lane.
ENABLE_DATASTORE = _env("ENABLE_DATASTORE", "auto").lower()


def datastore_path() -> str:
    """Full Discovery Engine resource path, or '' when the lane is unconfigured."""
    if not _DATASTORE_RAW:
        return ""
    if "/" in _DATASTORE_RAW:
        return _DATASTORE_RAW
    if not PROJECT_ID:
        return ""
    return (
        f"projects/{PROJECT_ID}/locations/{VERTEX_SEARCH_LOCATION}"
        f"/collections/default_collection/dataStores/{_DATASTORE_RAW}"
    )


DATASTORE_PATH = datastore_path()

# --- Uploaded-document lane -------------------------------------------------
ENABLE_DOCUMENTS = _flag("ENABLE_DOCUMENTS", True)

# Hard cap on how much text one read_document() call may return to the model.
# Uncapped reads are the fastest route back to a context blowup and to the
# MALFORMED_FUNCTION_CALL failure documented in docs/architecture.md.
MAX_DOCUMENT_READ_CHARS = int(_env("MAX_DOCUMENT_READ_CHARS", "24000"))

# How many retrieved passages search_documents() returns by default.
DEFAULT_SEARCH_TOP_K = int(_env("DEFAULT_SEARCH_TOP_K", "6"))
