"""Web app configuration.

Unlike coordinator/config.py and formatter_agent/config.py, this one is NOT
constrained to be self-contained — the web app is always run from the repo root
and is free to import the packages beside it. It reads the same root .env.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


HOST = _env("WEBAPP_HOST", "127.0.0.1")
PORT = int(_env("WEBAPP_PORT", "8000"))

APP_NAME = "financial-research-system"
STATIC_DIR = Path(__file__).resolve().parent / "static"

# --- Upload limits ----------------------------------------------------------
MAX_UPLOAD_MB = int(_env("MAX_UPLOAD_MB", "25"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DOCS_PER_SESSION = int(_env("MAX_DOCS_PER_SESSION", "5"))

ACCEPTED_MIME_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}

# --- OCR cache --------------------------------------------------------------
# Keyed by file hash. Re-uploading the same PDF while rehearsing a demo becomes
# instant instead of another 40 seconds of real Document AI spend.
OCR_CACHE_ENABLED = _flag("OCR_CACHE", True)
CACHE_DIR = ROOT / ".cache" / "ocr"

# --- Model / lanes (mirrors coordinator config, for the /api/health report) --
MODEL = _env("ADK_MODEL", "gemini-2.5-flash")
