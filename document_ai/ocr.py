"""Document AI OCR — the shared library.

Two consumers, one implementation:

  * `extract.py`      — the batch CLI, works on files in input_pdfs/
  * the web app       — works on uploaded bytes, in memory, never touching disk

`bytes` is therefore the primitive and file paths are the thin wrapper, not the
other way round. Forcing the web app to write a temp file just to satisfy a
path-shaped API would be backwards.

This module never prints and never reads configuration from the environment on
its own. Callers pass an `OcrConfig` and, optionally, a `progress` callback —
which is what lets the CLI print a live chunk counter while the web app updates
a progress bar, from the same code.

Why chunking exists at all: Document AI's *online* `process_document` accepts a
limited number of pages per request (15 for the standard OCR processor). A 200+
page annual report therefore has to be split. There is a `batch_process_documents`
LRO that takes bigger inputs, but it is Cloud Storage in and Cloud Storage out,
which would mean a bucket round-trip and a cleanup story for every upload. For
interactive use, splitting locally and fanning out is both simpler and faster.
"""

from __future__ import annotations

import io
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterator, Sequence

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

PDF_MIME = "application/pdf"

# Formats the OCR processor accepts. Only PDFs are ever split — a single image
# is one page by definition.
SUPPORTED_MIME_TYPES = frozenset(
    {PDF_MIME, "image/png", "image/jpeg", "image/tiff", "image/bmp", "image/webp"}
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class OcrConfig:
    """Everything needed to reach a Document AI processor.

    Frozen and hashable so `get_client` can cache the underlying gRPC client
    per configuration rather than rebuilding it for every document.
    """

    project_id: str
    processor_id: str
    location: str = "us"                 # processor region: "us" or "eu"
    max_pages_per_request: int = 15
    max_workers: int = 4

    @classmethod
    def from_env(cls) -> "OcrConfig":
        """Build from environment variables (see .env.example)."""
        return cls(
            project_id=(os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip(),
            processor_id=(os.environ.get("DOCAI_PROCESSOR_ID") or "").strip(),
            location=(os.environ.get("DOCAI_LOCATION") or "us").strip(),
            max_pages_per_request=int(os.environ.get("DOCAI_MAX_PAGES") or "15"),
            max_workers=int(os.environ.get("DOCAI_MAX_WORKERS") or "4"),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.project_id and self.processor_id)

    def missing(self) -> list[str]:
        """Names of the required settings that are unset — for error messages."""
        gaps = []
        if not self.project_id:
            gaps.append("GOOGLE_CLOUD_PROJECT")
        if not self.processor_id:
            gaps.append("DOCAI_PROCESSOR_ID")
        return gaps


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PageText:
    """Text of a single page, with its 1-based number in the original document.

    Page numbers are the reason this is a list rather than one joined string.
    Because we control the chunk boundaries we know each chunk's absolute page
    range, and Document AI gives per-page offsets within a chunk — so the page
    number survives all the way to a citation like "(10-K.pdf, p. 47)".
    """

    page_number: int
    text: str


@dataclass(frozen=True)
class OcrResult:
    pages: list[PageText] = field(default_factory=list)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n".join(page.text for page in self.pages)

    @property
    def char_count(self) -> int:
        return sum(len(page.text) for page in self.pages)


@dataclass(frozen=True)
class OcrProgress:
    chunks_done: int
    chunks_total: int
    pages_done: int
    pages_total: int


ProgressFn = Callable[[OcrProgress], None]


@dataclass(frozen=True)
class PdfChunk:
    """A slice of a PDF small enough for one online request."""

    data: bytes
    first_page: int   # 1-based, inclusive
    last_page: int    # 1-based, inclusive

    @property
    def page_count(self) -> int:
        return self.last_page - self.first_page + 1


class OcrError(RuntimeError):
    """Raised when a document cannot be processed."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
@lru_cache(maxsize=8)
def get_client(config: OcrConfig) -> documentai.DocumentProcessorServiceClient:
    """Document AI client pinned to the processor's regional endpoint.

    The endpoint is region-specific: a client built for the default global
    endpoint cannot see a processor created in "us".
    """
    opts = ClientOptions(api_endpoint=f"{config.location}-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def processor_name(config: OcrConfig) -> str:
    return get_client(config).processor_path(
        config.project_id, config.location, config.processor_id
    )


# ---------------------------------------------------------------------------
# PDF splitting
# ---------------------------------------------------------------------------
def count_pages(pdf_bytes: bytes) -> int:
    """Page count of a PDF, without sending it anywhere."""
    return len(PdfReader(io.BytesIO(pdf_bytes)).pages)


def iter_page_chunks(pdf_bytes: bytes, max_pages: int) -> Iterator[PdfChunk]:
    """Yield PDF slices of at most `max_pages` pages each.

    A PDF already within the limit yields exactly one chunk holding the original
    bytes — no re-encoding, so small files take the cheapest possible path.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)

    if total <= max_pages:
        yield PdfChunk(data=pdf_bytes, first_page=1, last_page=total)
        return

    for start in range(0, total, max_pages):
        end = min(start + max_pages, total)
        writer = PdfWriter()
        for page in reader.pages[start:end]:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        yield PdfChunk(data=buf.getvalue(), first_page=start + 1, last_page=end)


# ---------------------------------------------------------------------------
# Core OCR
# ---------------------------------------------------------------------------
def _pages_from_document(document, first_page: int) -> list[PageText]:
    """Split a Document AI response into per-page text.

    `document.text` is one flat string for the whole request; each page's layout
    carries `text_anchor.text_segments` holding [start, end) offsets into it.
    Slicing by those offsets is what recovers page boundaries.
    """
    full_text = document.text or ""
    pages: list[PageText] = []

    for offset, page in enumerate(document.pages):
        segments = getattr(getattr(page.layout, "text_anchor", None), "text_segments", None)
        if segments:
            chunks = []
            for segment in segments:
                start = int(segment.start_index or 0)
                end = int(segment.end_index or 0)
                if end > start:
                    chunks.append(full_text[start:end])
            page_text = "".join(chunks)
        else:
            page_text = ""
        pages.append(PageText(page_number=first_page + offset, text=page_text))

    # Defensive: if the response carried text but no usable page layout, keep the
    # text rather than silently returning empty pages.
    if full_text and not any(p.text for p in pages):
        return [PageText(page_number=first_page, text=full_text)]

    return pages


def _process_chunk(
    config: OcrConfig, name: str, data: bytes, mime_type: str, first_page: int
) -> list[PageText]:
    client = get_client(config)
    request = documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=data, mime_type=mime_type),
    )
    response = client.process_document(request=request)
    return _pages_from_document(response.document, first_page)


def ocr_bytes(
    data: bytes,
    *,
    config: OcrConfig,
    mime_type: str = PDF_MIME,
    progress: ProgressFn | None = None,
) -> OcrResult:
    """OCR a document already in memory.

    PDFs larger than the per-request page limit are split and the chunks are
    processed in parallel (`process_document` is a blocking call, so threads are
    the right tool), then reassembled in page order.
    """
    if not config.is_configured:
        raise OcrError(
            "Document AI is not configured. Missing: " + ", ".join(config.missing())
        )
    if mime_type not in SUPPORTED_MIME_TYPES:
        raise OcrError(
            f"Unsupported document type {mime_type!r}. "
            f"Supported: {', '.join(sorted(SUPPORTED_MIME_TYPES))}"
        )
    if not data:
        raise OcrError("Document is empty.")

    name = processor_name(config)

    # Non-PDFs are a single page by definition — no splitting.
    if mime_type != PDF_MIME:
        pages = _process_chunk(config, name, data, mime_type, first_page=1)
        if progress:
            progress(OcrProgress(1, 1, len(pages), len(pages)))
        return OcrResult(pages=pages)

    try:
        chunks: Sequence[PdfChunk] = list(
            iter_page_chunks(data, config.max_pages_per_request)
        )
    except Exception as exc:  # malformed / encrypted PDF
        raise OcrError(f"Could not read PDF: {type(exc).__name__}: {exc}") from exc

    if not chunks:
        raise OcrError("PDF contains no pages.")

    total_pages = chunks[-1].last_page
    done_chunks = 0
    done_pages = 0
    collected: list[list[PageText]] = [[] for _ in chunks]

    if progress:
        progress(OcrProgress(0, len(chunks), 0, total_pages))

    workers = max(1, min(config.max_workers, len(chunks)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _process_chunk, config, name, chunk.data, PDF_MIME, chunk.first_page
            ): index
            for index, chunk in enumerate(chunks)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                collected[index] = future.result()
            except Exception as exc:
                chunk = chunks[index]
                raise OcrError(
                    f"OCR failed on pages {chunk.first_page}-{chunk.last_page}: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
            done_chunks += 1
            done_pages += chunks[index].page_count
            if progress:
                progress(
                    OcrProgress(done_chunks, len(chunks), done_pages, total_pages)
                )

    # Reassemble in document order — completion order is not page order.
    pages = [page for group in collected for page in group]
    return OcrResult(pages=pages)


def ocr_path(
    path: str | Path,
    *,
    config: OcrConfig,
    progress: ProgressFn | None = None,
) -> OcrResult:
    """OCR a document on disk. Thin wrapper over `ocr_bytes`."""
    path = Path(path)
    mime_type = PDF_MIME if path.suffix.lower() == ".pdf" else _guess_mime(path)
    return ocr_bytes(
        path.read_bytes(), config=config, mime_type=mime_type, progress=progress
    )


def _guess_mime(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": PDF_MIME,
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
    }.get(suffix, PDF_MIME)
