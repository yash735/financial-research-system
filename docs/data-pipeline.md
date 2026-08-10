# Data pipeline

```
PDF  ──►  Document AI OCR  ──►  text  ──►  retrieval  ──►  Gemini  ──►  answer
          chunked, parallel              two paths       analysis
```

Conversion and analysis are separate concerns, and so is the retrieval step
between them. Document AI does the conversion in both paths; what differs is
where the text goes next.

## OCR

`document_ai/ocr.py` is the library; `document_ai/extract.py` is a batch CLI over
it and the web app calls the same functions on upload bytes.

```python
from document_ai.ocr import OcrConfig, ocr_bytes, ocr_path

result = ocr_bytes(pdf_bytes, config=OcrConfig.from_env())
result.page_count          # 97
result.text                # everything joined
result.pages[0].page_number, result.pages[0].text
```

Three things it handles:

**Page limits.** Document AI's *online* `process_document` accepts a limited
number of pages per request (15 for the standard OCR processor), so larger
filings are split with `pypdf`. There is a `batch_process_documents` LRO that
takes bigger inputs, but it is Cloud Storage in and Cloud Storage out — a bucket
round-trip and a cleanup obligation for every upload. Splitting locally is
simpler and faster for interactive use.

**Parallelism.** `process_document` blocks, so chunks fan out across a thread
pool (`DOCAI_MAX_WORKERS`) and are reassembled in page order. A 97-page annual
report takes about 15 seconds.

**Page numbers.** Because the chunk boundaries are ours, each chunk's absolute
page range is known, and `document.pages[i].layout.text_anchor.text_segments`
gives per-page offsets within the response text. That is what makes
`(report.pdf, p. 47)` citations possible instead of one anonymous blob.

`ocr.py` never prints. Callers pass a `progress` callback — the CLI draws a chunk
counter, the web app updates a progress bar, from the same code.

```bash
python document_ai/extract.py                 # everything in input_pdfs/
python document_ai/extract.py path/to/one.pdf
```

## The indexed corpus

Optional. The system runs uploads-only if it is absent, and the lane greys itself
out.

```bash
# 1. OCR your PDFs
python document_ai/extract.py

# 2. upload the TEXT — not the PDFs
gcloud storage cp document_ai/output_text/*.txt gs://<your-bucket>/ocr_text/

# 3. AI Applications console → Data Stores → Create data store
#      Source:    Cloud Storage → gs://<your-bucket>/ocr_text/
#      Data type: Unstructured documents
#      Location:  global
#      ID:        pick your own — see below

# 4. point the code at it
#    VERTEX_SEARCH_DATASTORE=<your-datastore-id>
```

The console is the practical route: Vertex AI Search has no stable `gcloud`
surface for datastore create/import.

### Index the OCR text, not the PDFs

This is the correctness point of the whole pipeline, and it was a real bug here.

An earlier version pointed the datastore at the **bucket root**, which held the
source PDFs, while the Document AI output sat in an `ocr_text/` subfolder. The
datastore ingests whatever path it is given — so retrieval silently ran on Vertex
AI Search's own parser and the OCR output was never used at all. Everything
worked; it was just answering from a different extraction than the one the
pipeline produced.

Point the datastore at the subfolder holding the `.txt` files. You can confirm
which one you have from the datastore's `unstructuredDataSize`: it should match
the byte total of your text files.

```bash
wc -c document_ai/output_text/*.txt        # compare with the API response
```

### Give it a deterministic id

Console-generated datastore ids carry a random suffix, so every rebuild changes
the resource path and forces a config change. Set your own id and rebuilds become
a no-op for the code.

## What costs money

| | |
|---|---|
| Document AI OCR | per page, once per document — the cache means a re-upload is free |
| Vertex AI Search | storage plus queries; a five-document corpus is within the free tier |
| GCS bucket | negligible |
| Gemini | per token, per question |

The OCR cache (`.cache/ocr/`, keyed by file hash) exists specifically so that
rehearsing a demo does not re-spend on the same file. `./run.sh --fresh` clears
it.

## Source documents

Not included in this repo — they are third-party copyrighted filings, and 11 MB
of them. Get your own from [SEC EDGAR](https://www.sec.gov/edgar/searchedgar/companysearch)
and drop them in `document_ai/input_pdfs/`.
