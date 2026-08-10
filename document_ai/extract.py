"""Run local PDFs through a Google Cloud Document AI OCR processor and save clean text.

Standalone utility for the Walmart-financials RAG pipeline: it takes the messy PDFs
in `document_ai/input_pdfs/`, sends them to your Document OCR processor, and writes the
extracted text to `document_ai/output_text/`. Those .txt files are what you then upload
to your Cloud Storage bucket and re-index in the Vertex AI Search datastore for cleaner
retrieval than raw-PDF parsing gives you.

Run from the project root with the venv that has the deps:
    adk_env/bin/python document_ai/extract.py
"""

import io
import os
from pathlib import Path

from dotenv import load_dotenv
from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from pypdf import PdfReader, PdfWriter

# Configuration comes from the repo-root .env (see .env.example). Nothing here
# is hardcoded, so the script is safe to publish and portable to any project.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROJECT_ID = (os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
LOCATION = (os.environ.get("DOCAI_LOCATION") or "us").strip()   # "us" or "eu"
PROCESSOR_ID = (os.environ.get("DOCAI_PROCESSOR_ID") or "").strip()

# Online (synchronous) process_document caps each request at 15 pages. Larger PDFs are
# split into <=15-page chunks, processed one chunk at a time, and the text is joined.
# DEMO-ASSUMPTION: 15 is the conservative online limit; bump if your processor allows more.
MAX_PAGES_PER_REQUEST = 15

MIME_TYPE = "application/pdf"

# Folders are resolved relative to this file so it runs from anywhere.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INPUT_DIR = os.path.join(SCRIPT_DIR, "input_pdfs")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "output_text")


def _make_client() -> documentai.DocumentProcessorServiceClient:
    """Document AI client pinned to the processor's regional endpoint."""
    opts = ClientOptions(api_endpoint=f"{LOCATION}-documentai.googleapis.com")
    return documentai.DocumentProcessorServiceClient(client_options=opts)


def _process_pdf_bytes(
    client: documentai.DocumentProcessorServiceClient,
    processor_name: str,
    pdf_bytes: bytes,
) -> str:
    """Send one <=15-page PDF chunk through the processor and return its text."""
    raw_doc = documentai.RawDocument(content=pdf_bytes, mime_type=MIME_TYPE)
    request = documentai.ProcessRequest(name=processor_name, raw_document=raw_doc)
    result = client.process_document(request=request)
    return result.document.text or ""


def _iter_page_chunks(pdf_path: str):
    """Yield PDF byte-chunks of at most MAX_PAGES_PER_REQUEST pages each.

    Small PDFs yield a single chunk (the whole file); large ones are split so every
    request stays under the online page limit.
    """
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    if total_pages <= MAX_PAGES_PER_REQUEST:
        with open(pdf_path, "rb") as fh:
            yield fh.read(), total_pages
        return

    for start in range(0, total_pages, MAX_PAGES_PER_REQUEST):
        writer = PdfWriter()
        for page in reader.pages[start:start + MAX_PAGES_PER_REQUEST]:
            writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        yield buf.getvalue(), total_pages


def extract_pdf(
    client: documentai.DocumentProcessorServiceClient,
    processor_name: str,
    pdf_path: str,
) -> str:
    """Full text for one PDF, transparently splitting multi-page files into chunks."""
    parts = []
    for chunk_bytes, total_pages in _iter_page_chunks(pdf_path):
        if total_pages > MAX_PAGES_PER_REQUEST:
            print(f"    (splitting {total_pages} pages into "
                  f"{MAX_PAGES_PER_REQUEST}-page chunks)")
        parts.append(_process_pdf_bytes(client, processor_name, chunk_bytes))
    return "\n".join(parts)


def main() -> None:
    if not PROJECT_ID:
        raise SystemExit(
            "GOOGLE_CLOUD_PROJECT is not set. Copy .env.example to .env and fill it in."
        )
    if not PROCESSOR_ID:
        raise SystemExit(
            "DOCAI_PROCESSOR_ID is not set. Copy .env.example to .env and fill it in "
            "(Console -> Document AI -> Processors -> your OCR processor -> Processor ID)."
        )

    os.makedirs(INPUT_DIR, exist_ok=True)
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    pdf_files = sorted(
        f for f in os.listdir(INPUT_DIR) if f.lower().endswith(".pdf")
    )
    if not pdf_files:
        raise SystemExit(f"No PDFs found in {INPUT_DIR}. Drop your Walmart PDFs there.")

    client = _make_client()
    processor_name = client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)

    print(f"Processor: {processor_name}")
    print(f"Found {len(pdf_files)} PDF(s) in {INPUT_DIR}\n")

    succeeded, failed = [], []
    for name in pdf_files:
        pdf_path = os.path.join(INPUT_DIR, name)
        out_path = os.path.join(OUTPUT_DIR, os.path.splitext(name)[0] + ".txt")
        print(f"Processing {name} ...")
        try:
            text = extract_pdf(client, processor_name, pdf_path)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"  -> {os.path.basename(out_path)}  ({len(text):,} chars)")
            succeeded.append((name, len(text)))
        except Exception as exc:  # one bad file must not kill the whole run
            print(f"  !! FAILED: {name} -- {type(exc).__name__}: {exc}")
            failed.append(name)

    print("\n" + "=" * 60)
    print(f"Done. {len(succeeded)} succeeded, {len(failed)} failed.")
    for name, n_chars in succeeded:
        print(f"  OK    {name}  ({n_chars:,} chars)")
    for name in failed:
        print(f"  FAIL  {name}")
    if succeeded:
        print(f"\nText files written to: {OUTPUT_DIR}")
        print("Upload these .txt files to your GCS bucket and re-index the datastore.")


if __name__ == "__main__":
    main()
