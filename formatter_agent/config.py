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
