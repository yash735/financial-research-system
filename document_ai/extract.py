"""Batch-OCR local PDFs through Document AI and save clean text.

Turns the PDFs in `document_ai/input_pdfs/` into text in `document_ai/output_text/`.
Those .txt files are what you upload to Cloud Storage and index in a Vertex AI
Search datastore — indexing OCR text rather than raw PDFs is what makes the
retrieval lane actually use this pipeline's output. See README.

    python document_ai/extract.py                 # every PDF in input_pdfs/
    python document_ai/extract.py path/to/one.pdf # just these

All the real work lives in ocr.py, which the web app shares. This file only
handles the filesystem and the terminal output.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

# Allow `python document_ai/extract.py` from the repo root as well as
# `python -m document_ai.extract`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from document_ai.ocr import (  # noqa: E402
    OcrConfig,
    OcrError,
    OcrProgress,
    count_pages,
    ocr_bytes,
)

SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_DIR = SCRIPT_DIR / "input_pdfs"
OUTPUT_DIR = SCRIPT_DIR / "output_text"

load_dotenv(SCRIPT_DIR.parent / ".env")


def _make_progress(page_total: int, chunk_total: int):
    """Per-chunk progress printer.

    The "splitting" banner is printed ONCE by the caller before processing
    starts, because count_pages() gives us the total up front. Earlier versions
    printed it inside the chunk loop, so a 218-page filing announced the split
    fifteen times.
    """

    def report(progress: OcrProgress) -> None:
        if chunk_total <= 1 or progress.chunks_done == 0:
            return
        print(
            f"    chunk {progress.chunks_done}/{progress.chunks_total}"
            f"  ({progress.pages_done}/{progress.pages_total} pages)",
            end="\r",
            flush=True,
        )

    return report


def extract_one(path: Path, config: OcrConfig) -> tuple[int, int]:
    """OCR one PDF and write its .txt. Returns (page_count, char_count)."""
    data = path.read_bytes()

    page_total = count_pages(data)
    chunk_total = -(-page_total // config.max_pages_per_request)  # ceiling division
    if chunk_total > 1:
        print(
            f"    splitting {page_total} pages into {chunk_total} chunks of "
            f"<={config.max_pages_per_request}, {config.max_workers} in parallel"
        )

    result = ocr_bytes(
        data, config=config, progress=_make_progress(page_total, chunk_total)
    )
    if chunk_total > 1:
        print(" " * 60, end="\r")  # clear the progress line

    out_path = OUTPUT_DIR / (path.stem + ".txt")
    out_path.write_text(result.text, encoding="utf-8")
    return result.page_count, result.char_count


def main() -> None:
    config = OcrConfig.from_env()
    if not config.is_configured:
        raise SystemExit(
            "Document AI is not configured. Copy .env.example to .env and set: "
            + ", ".join(config.missing())
            + "\n(Console -> Document AI -> Processors -> your OCR processor -> Processor ID)"
        )

    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        pdf_files = [Path(arg) for arg in sys.argv[1:]]
        missing = [p for p in pdf_files if not p.is_file()]
        if missing:
            raise SystemExit(f"Not found: {', '.join(str(p) for p in missing)}")
    else:
        pdf_files = sorted(p for p in INPUT_DIR.iterdir() if p.suffix.lower() == ".pdf")
        if not pdf_files:
            raise SystemExit(f"No PDFs found in {INPUT_DIR}. Drop some in there first.")

    print(f"Processor: {config.location}/{config.processor_id}")
    print(f"Found {len(pdf_files)} PDF(s)\n")

    succeeded: list[tuple[str, int, int]] = []
    failed: list[tuple[str, str]] = []

    for path in pdf_files:
        print(f"Processing {path.name} ...")
        try:
            pages, chars = extract_one(path, config)
            print(f"  -> {path.stem}.txt  ({pages} pages, {chars:,} chars)")
            succeeded.append((path.name, pages, chars))
        except Exception as exc:  # one bad file must not kill the whole run
            print(f"  !! FAILED: {path.name} -- {type(exc).__name__}: {exc}")
            failed.append((path.name, str(exc)))

    print("\n" + "=" * 60)
    print(f"Done. {len(succeeded)} succeeded, {len(failed)} failed.")
    for name, pages, chars in succeeded:
        print(f"  OK    {name}  ({pages} pages, {chars:,} chars)")
    for name, _ in failed:
        print(f"  FAIL  {name}")
    if succeeded:
        print(f"\nText written to: {OUTPUT_DIR}")
        print("Upload these .txt files to your bucket and index them — NOT the PDFs.")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
