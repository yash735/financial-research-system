"""In-memory store for uploaded documents.

WHY A PROCESS-GLOBAL DICT AND NOT SESSION STATE
-----------------------------------------------
A 97-page filing is ~380,000 characters. Putting that in ADK session state means
it is serialised into every event and carried through the whole agent graph — it
would dominate the payload of every turn. So the *text* lives here, keyed by an
opaque id, and only a small manifest (id, filename, page count) goes into session
state. The agent's tools then look the text up by id.

The safety property that makes a global dict acceptable: every tool validates the
requested doc_id against the manifest in *its own* session state before touching
this store. A session can only reach documents it was told about, so two browser
sessions cannot see each other's uploads even though they share the dict.

WHY IN MEMORY AND NOT ON DISK
-----------------------------
No cleanup obligation, no uploaded financial documents sitting in a temp folder,
nothing that can accidentally be committed. Uploads do not survive a restart,
which is the correct trade for an interactive tool.

Standard library only, so `coordinator/` remains self-contained and deployable
on its own.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .retrieval import Bm25Index, Chunk, Passage, chunk_pages

Status = Literal["processing", "ready", "failed"]

# Evict documents nobody has touched in this long. A demo session is minutes;
# two hours is generous without letting a long-running process grow unbounded.
DEFAULT_TTL_SECONDS = 2 * 60 * 60

# Total characters held across all documents before the oldest are evicted.
# ~20 annual reports' worth.
DEFAULT_MAX_TOTAL_CHARS = 8_000_000


@dataclass
class StoredDocument:
    doc_id: str
    filename: str
    mime_type: str
    status: Status = "processing"

    pages: list[tuple[int, str]] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    index: Bm25Index | None = None

    sha256: str = ""
    page_count: int = 0
    char_count: int = 0

    # Live OCR progress, driven by the ocr.py progress callback.
    pages_done: int = 0
    pages_total: int = 0

    error: str = ""
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    def manifest(self) -> dict:
        """The small record that goes into session state and to the browser."""
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "status": self.status,
            "page_count": self.page_count,
            "char_count": self.char_count,
            "pages_done": self.pages_done,
            "pages_total": self.pages_total,
            "error": self.error,
        }


class DocumentStore:
    def __init__(
        self,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ) -> None:
        self._docs: dict[str, StoredDocument] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds
        self._max_total_chars = max_total_chars

    # -- lifecycle ----------------------------------------------------------
    def create(self, filename: str, mime_type: str, data: bytes) -> StoredDocument:
        """Register a document as `processing` before OCR starts."""
        doc = StoredDocument(
            doc_id=uuid.uuid4().hex,
            filename=filename,
            mime_type=mime_type,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        with self._lock:
            self._docs[doc.doc_id] = doc
        return doc

    def set_progress(self, doc_id: str, pages_done: int, pages_total: int) -> None:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc:
                doc.pages_done = pages_done
                doc.pages_total = pages_total

    def mark_ready(self, doc_id: str, pages: list[tuple[int, str]]) -> None:
        """Attach OCR output and build the search index."""
        chunks = chunk_pages(pages)
        index = Bm25Index.build(chunks)
        with self._lock:
            doc = self._docs.get(doc_id)
            if not doc:
                return
            doc.pages = pages
            doc.chunks = chunks
            doc.index = index
            doc.page_count = len(pages)
            doc.char_count = sum(len(text) for _, text in pages)
            doc.pages_done = doc.page_count
            doc.pages_total = doc.page_count
            doc.status = "ready"
            doc.last_used_at = time.time()
        self._evict_if_needed()

    def mark_failed(self, doc_id: str, error: str) -> None:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc:
                doc.status = "failed"
                doc.error = error

    def delete(self, doc_id: str) -> bool:
        with self._lock:
            return self._docs.pop(doc_id, None) is not None

    # -- reads --------------------------------------------------------------
    def get(self, doc_id: str) -> StoredDocument | None:
        with self._lock:
            doc = self._docs.get(doc_id)
            if doc:
                doc.last_used_at = time.time()
            return doc

    def get_many(self, doc_ids: list[str]) -> list[StoredDocument]:
        return [doc for doc in (self.get(i) for i in doc_ids) if doc is not None]

    # -- search -------------------------------------------------------------
    def search(self, doc_ids: list[str], query: str, top_k: int = 6) -> list[Passage]:
        """Best passages across the given documents, highest score first.

        Scores from separate per-document indexes are compared directly. They are
        not strictly commensurable — idf is computed within each document — but
        for a handful of similar filings the ranking is good enough, and it keeps
        each document independently addable and removable without a reindex.
        """
        hits: list[Passage] = []
        for doc in self.get_many(doc_ids):
            if doc.status != "ready" or doc.index is None:
                continue
            for chunk_index, score in doc.index.search(query, top_k=top_k):
                chunk = doc.chunks[chunk_index]
                hits.append(
                    Passage(
                        doc_id=doc.doc_id,
                        filename=doc.filename,
                        page_number=chunk.page_number,
                        text=chunk.text,
                        score=score,
                    )
                )
        hits.sort(key=lambda p: p.score, reverse=True)
        return hits[:top_k]

    def read_pages(
        self, doc_id: str, start_page: int | None = None, end_page: int | None = None
    ) -> list[tuple[int, str]]:
        doc = self.get(doc_id)
        if not doc or doc.status != "ready":
            return []
        first = start_page or 1
        last = end_page or doc.page_count
        return [(n, t) for n, t in doc.pages if first <= n <= last]

    # -- housekeeping -------------------------------------------------------
    def _evict_if_needed(self) -> None:
        """Drop expired documents, then the least recently used over the cap."""
        now = time.time()
        with self._lock:
            for doc_id, doc in list(self._docs.items()):
                if now - doc.last_used_at > self._ttl:
                    self._docs.pop(doc_id, None)

            total = sum(doc.char_count for doc in self._docs.values())
            if total <= self._max_total_chars:
                return

            by_age = sorted(self._docs.values(), key=lambda d: d.last_used_at)
            for doc in by_age:
                if total <= self._max_total_chars:
                    break
                self._docs.pop(doc.doc_id, None)
                total -= doc.char_count


# One store per process. Import this, don't build your own.
store = DocumentStore()
