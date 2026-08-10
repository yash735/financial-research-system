"""Upload handling: validate, OCR, index.

Uploads return immediately with a document id and a `processing` status; OCR
runs in the background and the browser polls. A 200-page filing is ~14 Document
AI round-trips, and holding the HTTP request open for that long invites a proxy
or browser timeout at the worst possible moment. Polling a small JSON status is
boring and bulletproof, and it keeps SSE as the single streaming mechanism in
the app.

Nothing is written to disk except the OCR cache, which stores extracted text
keyed by file hash. The uploaded bytes themselves never touch the filesystem.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging

from coordinator.document_store import StoredDocument, store
from document_ai.ocr import OcrConfig, OcrError, OcrProgress, ocr_bytes

from . import config
from .state import BrowserSession

log = logging.getLogger(__name__)

# Leading bytes that actually identify the format. Browsers lie about
# content-type and filenames are user input; the magic number is the only
# trustworthy signal.
_MAGIC = {
    b"%PDF-": "application/pdf",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"II*\x00": "image/tiff",
    b"MM\x00*": "image/tiff",
}


class UploadError(ValueError):
    """Rejected before any work is done. Message is shown to the user."""


def detect_mime(data: bytes) -> str:
    for signature, mime in _MAGIC.items():
        if data.startswith(signature):
            return mime
    raise UploadError(
        "Unrecognised file type. Upload a PDF, PNG, JPEG or TIFF — the file's "
        "contents did not match any of those."
    )


def validate(filename: str, data: bytes, session: BrowserSession) -> str:
    if not data:
        raise UploadError("That file is empty.")

    if len(data) > config.MAX_UPLOAD_BYTES:
        raise UploadError(
            f"{filename} is {len(data) / 1024 / 1024:.1f} MB, over the "
            f"{config.MAX_UPLOAD_MB} MB limit."
        )

    live = [d for d in session.manifest() if d["status"] != "failed"]
    if len(live) >= config.MAX_DOCS_PER_SESSION:
        raise UploadError(
            f"You already have {len(live)} documents loaded (limit "
            f"{config.MAX_DOCS_PER_SESSION}). Remove one first."
        )

    return detect_mime(data)


# ---------------------------------------------------------------------------
# OCR cache
# ---------------------------------------------------------------------------
def _cache_path(digest: str):
    return config.CACHE_DIR / f"{digest}.json"


def _cache_read(digest: str) -> list[tuple[int, str]] | None:
    if not config.OCR_CACHE_ENABLED:
        return None
    path = _cache_path(digest)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [(int(p["n"]), p["t"]) for p in payload["pages"]]
    except Exception:
        return None  # corrupt cache entry is not an error, just a miss


def _cache_write(digest: str, pages: list[tuple[int, str]]) -> None:
    if not config.OCR_CACHE_ENABLED:
        return
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_path(digest).write_text(
            json.dumps({"pages": [{"n": n, "t": t} for n, t in pages]}),
            encoding="utf-8",
        )
    except Exception as exc:
        log.warning("Could not write OCR cache: %s", exc)


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------
def _run_ocr_blocking(
    doc_id: str, data: bytes, mime_type: str, ocr_config: OcrConfig
) -> None:
    """Runs on a worker thread — process_document is a blocking call."""
    digest = hashlib.sha256(data).hexdigest()

    cached = _cache_read(digest)
    if cached is not None:
        store.mark_ready(doc_id, cached)
        return

    def on_progress(progress: OcrProgress) -> None:
        store.set_progress(doc_id, progress.pages_done, progress.pages_total)

    result = ocr_bytes(
        data, config=ocr_config, mime_type=mime_type, progress=on_progress
    )
    pages = [(page.page_number, page.text) for page in result.pages]
    _cache_write(digest, pages)
    store.mark_ready(doc_id, pages)


async def start_ingest(
    filename: str,
    data: bytes,
    session: BrowserSession,
    ocr_config: OcrConfig,
) -> StoredDocument:
    """Validate, register, and kick off OCR. Returns immediately."""
    mime_type = validate(filename, data, session)

    doc = store.create(filename=filename, mime_type=mime_type, data=data)
    session.doc_ids.append(doc.doc_id)

    async def worker() -> None:
        try:
            await asyncio.to_thread(
                _run_ocr_blocking, doc.doc_id, data, mime_type, ocr_config
            )
        except OcrError as exc:
            store.mark_failed(doc.doc_id, str(exc))
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            log.exception("OCR failed for %s", filename)
            store.mark_failed(doc.doc_id, f"{type(exc).__name__}: {exc}")

    asyncio.create_task(worker())
    return doc


def remove(doc_id: str, session: BrowserSession) -> bool:
    """Forget a document, both from the session and the store."""
    if doc_id not in session.doc_ids:
        return False
    session.doc_ids.remove(doc_id)
    store.delete(doc_id)
    return True
