#!/usr/bin/env python3
"""Ask the coordinator a question from the terminal, showing the routing.

Useful for checking the agent pipeline without the browser — if an answer is
wrong here, the bug is in the agents; if it is right here but wrong in the UI,
the bug is in the web layer.

    python scripts/ask.py "What was total revenue in the most recent fiscal year?"
    python scripts/ask.py --local "..."          # force the in-process formatter
    python scripts/ask.py --doc report.pdf "..." # OCR a file first, then ask

Replaces an older script that queried a hardcoded Agent Engine deployment; this
one drives the local agent, so there is no cloud resource to go stale.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.utils.context_utils import Aclosing  # noqa: E402
from google.genai import types  # noqa: E402

from coordinator import config  # noqa: E402
from coordinator.agent import build_root_agent  # noqa: E402
from coordinator.document_store import store  # noqa: E402
from coordinator.health import probe_datastore, probe_formatter  # noqa: E402
from document_ai.ocr import OcrConfig, ocr_bytes  # noqa: E402

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def load_document(path: Path) -> dict:
    ocr_config = OcrConfig.from_env()
    if not ocr_config.is_configured:
        raise SystemExit("Document AI is not configured: " + ", ".join(ocr_config.missing()))

    print(f"{DIM}OCR {path.name}…{RESET}", end=" ", flush=True)
    started = time.time()
    data = path.read_bytes()
    doc = store.create(path.name, "application/pdf", data)
    result = ocr_bytes(data, config=ocr_config)
    store.mark_ready(doc.doc_id, [(p.page_number, p.text) for p in result.pages])
    record = store.get(doc.doc_id)
    print(f"{record.page_count} pages, {record.char_count:,} chars in {time.time()-started:.1f}s")
    return record.manifest()


async def run(question: str, formatter: str, manifest: list[dict]) -> None:
    datastore_ok = (
        config.ENABLE_DATASTORE != "off"
        and bool(config.DATASTORE_PATH)
        and probe_datastore(config.DATASTORE_PATH, config.PROJECT_ID)
    )

    root = build_root_agent(
        formatter=formatter,
        enable_datastore=datastore_ok,
        enable_documents=True,
    )
    print(
        f"{DIM}lanes: documents=on datastore={'on' if datastore_ok else 'off'} "
        f"market=on · formatter={formatter}{RESET}\n"
    )

    service = InMemorySessionService()
    runner = Runner(app_name="ask", agent=root, session_service=service)
    await service.create_session(app_name="ask", user_id="cli", session_id="cli")

    summary = ", ".join(f"{d['filename']} ({d['page_count']} pages)" for d in manifest) or "none"
    started = time.time()
    answer = ""

    try:
        async with Aclosing(
            runner.run_async(
                user_id="cli",
                session_id="cli",
                new_message=types.Content(role="user", parts=[types.Part(text=question)]),
                state_delta={"uploaded_docs": manifest, "uploaded_docs_summary": summary},
            )
        ) as events:
            async for event in events:
                elapsed = time.time() - started
                for call in event.get_function_calls() or []:
                    print(f"{DIM}[{elapsed:5.1f}s] → {call.name}{RESET}")
                for response in event.get_function_responses() or []:
                    print(f"{DIM}[{elapsed:5.1f}s] ← {response.name}{RESET}")
                if event.content and event.content.parts and not event.partial:
                    text = "".join(p.text or "" for p in event.content.parts).strip()
                    if text and event.author == "formatter_agent":
                        answer = text
    finally:
        await runner.close()

    print(f"\n{BOLD}Answer{RESET} {DIM}({time.time()-started:.1f}s){RESET}\n")
    print(answer or "(no answer returned)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question", nargs="?", help="the question to ask")
    parser.add_argument("--doc", type=Path, help="OCR this file and ask about it")
    parser.add_argument("--local", action="store_true", help="use the in-process formatter")
    args = parser.parse_args()

    if not args.question:
        parser.error("give me a question to ask")

    manifest = [load_document(args.doc)] if args.doc else []

    formatter = "local"
    if not args.local:
        status = probe_formatter(config.FORMATTER_A2A_URL, timeout=3.0)
        formatter = "remote" if status.reachable else "local"
        if not status.reachable:
            print(f"{DIM}remote formatter unreachable, using the fallback{RESET}")

    asyncio.run(run(args.question, formatter, manifest))


if __name__ == "__main__":
    main()
