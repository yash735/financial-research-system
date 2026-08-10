"""Passage retrieval over uploaded documents — BM25, standard library only.

WHY NOT JUST PUT THE WHOLE DOCUMENT IN THE PROMPT
-------------------------------------------------
An annual report OCRs to roughly 380,000 characters — about 95,000 tokens. Three
uploaded filings would be ~285,000 tokens *per turn*, re-sent on every follow-up
question. That is tens of seconds to first token and it re-bills the entire
corpus each time. Retrieval keeps a turn to a few thousand tokens.

WHY BM25 AND NOT EMBEDDINGS
---------------------------
An embedding lane means an extra API call per chunk at ingest time — more
latency, more cost, and another thing that can fail mid-demo. BM25 indexes a
five-document corpus in about a second with no network at all, and financial
questions are unusually keyword-shaped ("operating margin", "FY2023",
"Sam's Club"), which is exactly where lexical matching is strongest.

WHY NOT NAIVE SUBSTRING SEARCH
------------------------------
The phrase a user types rarely appears verbatim next to the number they want.
Substring matching produces confident misses; BM25 ranks by term rarity, so a
query mentioning "FY2023" pulls the pages where that specific year is discussed
rather than the first page that happens to contain the word "revenue".

This module is deliberately dependency-free so that `coordinator/` stays
self-contained and deployable on its own.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# Passage sizing. Chunks never cross a page boundary, so every hit can cite an
# exact page. ~1200 chars is a few paragraphs: big enough to carry a table row
# with its header, small enough that a handful fit in a prompt.
TARGET_CHUNK_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200
MIN_CHUNK_CHARS = 80

# Query terms that name a specific figure or period are worth more than ordinary
# prose words. BM25's idf already favours rare terms; this nudges numeric and
# fiscal-year tokens further, because in a filing the difference between the
# right answer and a plausible wrong one is usually the year.
NUMERIC_TERM_BOOST = 1.6

_FISCAL_YEAR_RE = re.compile(r"^(?:fy)?(?:19|20)\d{2}$")
_NUMERIC_RE = re.compile(r"^\d")

# "fy2023" must survive as one token, so alphabetic-then-numeric runs are matched
# before bare words. Numbers keep their separators during matching and are
# normalised afterwards, so "611,289" and "611289" collide.
_TOKEN_RE = re.compile(r"[a-z]+[0-9]+|[a-z]+|[0-9][0-9,.]*")

_STOPWORDS = frozenset(
    """
    a an and are as at be been but by for from had has have he her his if in into is it
    its of on or our so that the their them then there these they this to was were what
    when where which while who will with would you your
    """.split()
)


def tokenize(text: str) -> list[str]:
    """Lowercase word/number tokens, stopwords dropped, numbers normalised."""
    tokens = []
    for raw in _TOKEN_RE.findall(text.lower()):
        token = raw.rstrip(".,")
        if not token or token in _STOPWORDS:
            continue
        if _NUMERIC_RE.match(token):
            token = token.replace(",", "")
        tokens.append(token)
    return tokens


@dataclass(frozen=True)
class Chunk:
    """A retrievable passage, anchored to the page it came from."""

    index: int
    page_number: int
    text: str


@dataclass
class Passage:
    """A search hit: the chunk, its score, and where it came from."""

    doc_id: str
    filename: str
    page_number: int
    text: str
    score: float


def chunk_pages(pages: list[tuple[int, str]]) -> list[Chunk]:
    """Split (page_number, text) pairs into overlapping passages.

    Chunks never span pages. Overlap keeps a sentence that straddles a boundary
    retrievable from both sides.
    """
    chunks: list[Chunk] = []
    index = 0

    for page_number, text in pages:
        cleaned = text.strip()
        if not cleaned:
            continue

        if len(cleaned) <= TARGET_CHUNK_CHARS:
            chunks.append(Chunk(index=index, page_number=page_number, text=cleaned))
            index += 1
            continue

        start = 0
        while start < len(cleaned):
            end = min(start + TARGET_CHUNK_CHARS, len(cleaned))

            # Prefer to break at a paragraph or sentence boundary so passages
            # read as coherent text rather than stopping mid-word.
            if end < len(cleaned):
                window = cleaned[start:end]
                for marker in ("\n\n", "\n", ". "):
                    cut = window.rfind(marker)
                    if cut > TARGET_CHUNK_CHARS // 2:
                        end = start + cut + len(marker)
                        break

            piece = cleaned[start:end].strip()
            if len(piece) >= MIN_CHUNK_CHARS or not chunks:
                chunks.append(Chunk(index=index, page_number=page_number, text=piece))
                index += 1

            if end >= len(cleaned):
                break
            start = max(end - CHUNK_OVERLAP_CHARS, start + 1)

    return chunks


@dataclass
class Bm25Index:
    """Okapi BM25 over a fixed set of chunks.

    Built once at ingest time; queries are pure arithmetic over small dicts, so
    a search is sub-millisecond for a corpus this size.
    """

    k1: float = 1.5
    b: float = 0.75

    _doc_freq: Counter = field(default_factory=Counter)
    _term_freqs: list[Counter] = field(default_factory=list)
    _lengths: list[int] = field(default_factory=list)
    _avg_length: float = 0.0
    _count: int = 0

    @classmethod
    def build(cls, chunks: list[Chunk]) -> "Bm25Index":
        index = cls()
        for chunk in chunks:
            tokens = tokenize(chunk.text)
            freqs = Counter(tokens)
            index._term_freqs.append(freqs)
            index._lengths.append(len(tokens))
            for term in freqs:
                index._doc_freq[term] += 1
        index._count = len(chunks)
        index._avg_length = (
            sum(index._lengths) / index._count if index._count else 0.0
        )
        return index

    def _idf(self, term: str) -> float:
        n = self._doc_freq.get(term, 0)
        if n == 0:
            return 0.0
        return math.log(1 + (self._count - n + 0.5) / (n + 0.5))

    def search(self, query: str, top_k: int = 6) -> list[tuple[int, float]]:
        """Return (chunk_index, score) for the best matches, highest first."""
        query_terms = tokenize(query)
        if not query_terms or not self._count:
            return []

        weights: dict[str, float] = {}
        for term in query_terms:
            boost = (
                NUMERIC_TERM_BOOST
                if _FISCAL_YEAR_RE.match(term) or _NUMERIC_RE.match(term)
                else 1.0
            )
            weights[term] = weights.get(term, 0.0) + boost

        scored: list[tuple[int, float]] = []
        for i, freqs in enumerate(self._term_freqs):
            score = 0.0
            length = self._lengths[i] or 1
            norm = self.k1 * (1 - self.b + self.b * length / (self._avg_length or 1))
            for term, weight in weights.items():
                tf = freqs.get(term)
                if not tf:
                    continue
                score += weight * self._idf(term) * (tf * (self.k1 + 1)) / (tf + norm)
            if score > 0:
                scored.append((i, score))

        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored[:top_k]
