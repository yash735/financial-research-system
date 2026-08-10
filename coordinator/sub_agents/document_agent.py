"""document_agent — answers from documents the user uploaded in this session.

Holds ordinary FunctionTools rather than a built-in grounding tool, so it is
free of the "search tools cannot mix with other tools" restriction that forces
market_agent and financials_agent to stay isolated.

WHY TOOLS INSTEAD OF PUTTING THE TEXT IN THE INSTRUCTION
--------------------------------------------------------
Three uploaded filings injected wholesale would be ~285,000 tokens on every
turn. With tools the model retrieves only the passages it needs, the cost is
flat as documents are added, and — the part that matters for a live demo — the
retrieval shows up as a visible step in the trace instead of happening silently
inside a giant prompt.

SESSION ISOLATION
-----------------
Document text lives in a process-global store. Every tool here first reads the
manifest of documents belonging to *this* session from session state, and
refuses any doc_id not in it. A session can therefore only ever reach its own
uploads.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.tools import FunctionTool, ToolContext

from .. import config
from ..document_store import store

# Session-state key holding this session's document manifest. The web app writes
# it with state_delta on every turn; see webapp/chat.py.
DOCS_STATE_KEY = "uploaded_docs"


def _session_docs(tool_context: ToolContext) -> list[dict]:
    docs = tool_context.state.get(DOCS_STATE_KEY) or []
    return [d for d in docs if isinstance(d, dict) and d.get("doc_id")]


def _allowed_ids(tool_context: ToolContext) -> set[str]:
    return {d["doc_id"] for d in _session_docs(tool_context)}


def list_uploaded_documents(tool_context: ToolContext) -> dict:
    """List the documents the user has uploaded in this conversation.

    Call this first when a question refers to "the document", "the filing", "my
    upload", or a specific filename, so you know what is available and what each
    document's id is.

    Returns:
        A dict with a "documents" list. Each entry has doc_id, filename,
        page_count and char_count. An empty list means nothing has been uploaded.
    """
    docs = [
        {
            "doc_id": d["doc_id"],
            "filename": d.get("filename", "unknown"),
            "page_count": d.get("page_count", 0),
            "char_count": d.get("char_count", 0),
        }
        for d in _session_docs(tool_context)
        if d.get("status", "ready") == "ready"
    ]
    return {"documents": docs, "count": len(docs)}


def search_documents(
    query: str,
    tool_context: ToolContext,
    doc_id: str | None = None,
    top_k: int | None = None,
) -> dict:
    """Find the passages most relevant to a query inside the uploaded documents.

    This is the main way to answer questions about an uploaded filing. Search
    with the specific financial terms you need — for example "operating margin
    fiscal 2024", "total revenue", "risk factors competition" — rather than
    repeating the user's whole sentence. Call it several times with different
    phrasings if the first result set is thin.

    Args:
        query: The financial terms to search for.
        doc_id: Restrict to one document. Omit to search all uploaded documents.
        top_k: How many passages to return. Defaults to a sensible value.

    Returns:
        A dict with a "passages" list, each carrying filename, page_number and
        text. Always cite the filename and page number when you use a passage.
    """
    allowed = _allowed_ids(tool_context)
    if not allowed:
        return {"passages": [], "count": 0, "note": "No documents have been uploaded."}

    if doc_id:
        if doc_id not in allowed:
            return {
                "passages": [],
                "count": 0,
                "error": f"Unknown doc_id {doc_id!r}. Call list_uploaded_documents first.",
            }
        target_ids = [doc_id]
    else:
        target_ids = list(allowed)

    limit = top_k or config.DEFAULT_SEARCH_TOP_K
    limit = max(1, min(limit, 20))

    passages = store.search(target_ids, query, top_k=limit)
    return {
        "passages": [
            {
                "filename": p.filename,
                "page_number": p.page_number,
                "text": p.text,
            }
            for p in passages
        ],
        "count": len(passages),
    }


def read_document(
    doc_id: str,
    tool_context: ToolContext,
    start_page: int | None = None,
    end_page: int | None = None,
) -> dict:
    """Read a page range of an uploaded document in full.

    Use this only when search_documents is not enough — for example when you
    need a whole section in order, or the surrounding context of a table. Prefer
    search for targeted questions. Long ranges are truncated, so read in slices.

    Args:
        doc_id: Which document to read.
        start_page: First page (1-based). Defaults to the beginning.
        end_page: Last page, inclusive. Defaults to the end.

    Returns:
        A dict with the text, the page range actually returned, and a "truncated"
        flag telling you whether to call again with a later start_page.
    """
    if doc_id not in _allowed_ids(tool_context):
        return {
            "text": "",
            "error": f"Unknown doc_id {doc_id!r}. Call list_uploaded_documents first.",
        }

    pages = store.read_pages(doc_id, start_page, end_page)
    if not pages:
        return {"text": "", "error": "No pages in that range, or the document is not ready."}

    # Cap what a single call can return. An uncapped read pushes the whole
    # document up through the gatherer and into the formatter, which is exactly
    # the oversized-payload situation this architecture exists to avoid.
    budget = config.MAX_DOCUMENT_READ_CHARS
    kept: list[str] = []
    used = 0
    last_page = pages[0][0]
    truncated = False

    for number, text in pages:
        block = f"[p. {number}]\n{text}"
        if used + len(block) > budget and kept:
            truncated = True
            break
        kept.append(block)
        used += len(block)
        last_page = number

    doc = store.get(doc_id)
    return {
        "filename": doc.filename if doc else "",
        "text": "\n\n".join(kept),
        "first_page": pages[0][0],
        "last_page": last_page,
        "total_pages": doc.page_count if doc else 0,
        "truncated": truncated,
        "note": (
            f"Truncated at page {last_page}. Call again with start_page="
            f"{last_page + 1} to continue."
            if truncated
            else ""
        ),
    }


# How hard to look. This is the one genuine speed-versus-thoroughness trade in
# the system, so it is exposed as the Deep research toggle rather than being
# decided once for everyone.
_SEARCH_NORMAL = (
    "2. Use search_documents with precise financial terms rather than the "
    "user's whole sentence. Put ALL the terms you need into one query — the "
    "search ranks by term overlap, so 'total revenues operating income fiscal "
    "2025' finds the statements page in a single call. Prefer one or two "
    "well-chosen searches over many narrow ones.\n"
    "3. Only search again if the first results genuinely lack what you need, "
    "and when you do, change vocabulary rather than repeating yourself — a "
    "filing may say 'operating income', 'operating profit', or 'income from "
    "operations' for the same line.\n"
)

_SEARCH_DEEP = (
    "2. Use search_documents with precise financial terms rather than the "
    "user's whole sentence. Search AS MANY TIMES AS YOU NEED — thoroughness "
    "matters more than speed here, and a missed figure is far worse than a slow "
    "answer.\n"
    "3. Actively try alternative vocabulary before concluding something is "
    "absent: a filing may say 'operating income', 'operating profit', or "
    "'income from operations' for the same line, and may report a figure in a "
    "statements table, an MD&A discussion, and a segment note with different "
    "wording each time. Search for the figure, then search again for the "
    "surrounding discussion so you can explain it rather than just quote it.\n"
    "3b. Cross-check anything that looks surprising against a second mention in "
    "the document before reporting it.\n"
)

_INSTRUCTION_HEAD = (
    "You are a financial analyst working from documents the user uploaded in "
    "this conversation. They are typically annual reports, 10-K/10-Q filings, "
    "or investor presentations, converted to text by OCR.\n\n"
    "How to work:\n"
    "1. If you do not already know what is available, call "
    "list_uploaded_documents.\n"
)

_INSTRUCTION_TAIL = (
    "4. Use read_document only when you need a full section in order.\n\n"
)

_ANSWER_RULES = (
    "How to answer:\n"
    "- Report what the documents actually say, with the figures as printed. "
    "Include units and the period the figure covers.\n"
    "- BE EXACT ABOUT WHICH LINE ITEM YOU ARE QUOTING. In a filing these are "
    "different numbers and they are not interchangeable: 'net sales' excludes "
    "membership and other income, while 'total revenues' includes it; 'gross "
    "profit' is not 'operating income'; 'operating income' is not 'net income'. "
    "If the user asks for total revenue, give the line labelled total revenues — "
    "do not substitute net sales. If the document reports both and you are "
    "unsure which the user meant, give both and label them.\n"
    "- Watch the units and the scale. Filings mix 'in millions' and 'in "
    "billions' between tables, and a figure copied without its scale is wrong "
    "by a factor of a thousand. State the units you found.\n"
    "- CITE EVERY FIGURE with its filename and page, like "
    "(annual-report.pdf, p. 47). This is not optional — the citation is what "
    "makes the answer checkable.\n"
    "- Quote short excerpts where the exact wording matters, but do NOT dump "
    "large blocks of the document. Summarise and cite instead.\n"
    "- If the documents do not contain the answer, say so plainly. Never fill "
    "the gap from general knowledge — you are the evidence-from-this-document "
    "specialist, and a fabricated figure is worse than a gap.\n"
    "- OCR text can be imperfect. If a number looks garbled or a table has "
    "lost its structure, say what you can see and flag the uncertainty."
)


def build_instruction(*, deep: bool = False) -> str:
    return (
        _INSTRUCTION_HEAD
        + (_SEARCH_DEEP if deep else _SEARCH_NORMAL)
        + _INSTRUCTION_TAIL
        + _ANSWER_RULES
    )


def build_document_agent(*, deep: bool = False) -> Agent:
    budget = (
        config.THINKING_SPECIALIST_DEEP if deep else config.THINKING_SPECIALIST_NORMAL
    )
    return Agent(
        name="document_agent",
        model=config.MODEL,
        description=(
            "Answers questions about financial documents the user uploaded in "
            "this session, which have been OCR'd and indexed for passage retrieval."
        ),
        instruction=build_instruction(deep=deep),
        tools=[
            FunctionTool(list_uploaded_documents),
            FunctionTool(search_documents),
            FunctionTool(read_document),
        ],
        generate_content_config=config.thinking_config(budget),
    )
