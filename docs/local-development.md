# Local development

## Setup

```bash
python -m venv adk_env
./adk_env/bin/python -m pip install -r requirements.txt

gcloud auth application-default login
gcloud auth application-default set-quota-project <your-project-id>
gcloud services enable aiplatform.googleapis.com documentai.googleapis.com discoveryengine.googleapis.com

cp .env.example .env      # fill in the REQUIRED values
```

`set-quota-project` is not optional if you use the indexed-filings lane. Without
it, Discovery Engine bills the request to a shared consumer project and returns
`403 SERVICE_DISABLED` on a perfectly healthy datastore — a failure that looks
exactly like a missing datastore.

## Running

```bash
./run.sh                  # formatter service + web app  →  http://127.0.0.1:8000
./run.sh --no-formatter   # web app only, exercising the in-process fallback
./run.sh --fresh          # clear the OCR cache first
./run.sh --port 9000
```

Rehearse `--no-formatter` at least once before a demo. It is the path you land on
if the formatter is down, and you want to have seen it.

Other entry points:

```bash
# ask from the terminal, with routing shown — no browser involved
python scripts/ask.py "What was total revenue in the most recent fiscal year?"
python scripts/ask.py --doc report.pdf "Summarise the risk factors"
python scripts/ask.py --local "..."        # force the in-process formatter

# ADK's own developer UI, for inspecting the raw agent graph
python -m google.adk.cli web .

# batch-OCR the PDFs in document_ai/input_pdfs/
python document_ai/extract.py

# guard the duplicated analyst prompt
python scripts/check_prompt_sync.py
```

## Always use `python -m`

Never call the venv's console scripts (`adk`, `uvicorn`, `pip`) directly. They
hardcode an absolute interpreter path in their shebang, so they break the moment
the virtualenv is moved — and a shebang cannot express a path containing a space
at all, so they will never work from a directory like `My Projects/`.

```bash
./adk_env/bin/uvicorn ...            # bad interpreter: no such file or directory
./adk_env/bin/python -m uvicorn ...  # always works
```

`run.sh` does this throughout.

## Layout

```
coordinator/          routing agent, specialists, retrieval, capability probes
  agent.py            build_root_agent() — assembles the graph from live lanes
  config.py           env configuration (self-contained; see below)
  prompts.py          router instruction, composed per lane
  document_store.py   in-memory uploads, TTL and byte caps
  retrieval.py        BM25 over page-anchored passages
  health.py           formatter-card and datastore probes
  formatter_fallback.py
  sub_agents/         document_agent · financials_agent · market_agent
formatter_agent/      standalone A2A analyst service
document_ai/
  ocr.py              the OCR library (bytes in, per-page text out)
  extract.py          batch CLI over it
webapp/
  main.py api.py state.py ingest.py trace.py config.py
  static/             index.html · app.css · app.js · md.js
k8s/                  GKE: one command up, one command down
scripts/              ask.py · check_prompt_sync.py
```

`coordinator/` and `formatter_agent/` each carry their own `config.py` and their
own copy of the analyst prompt. That is deliberate: each is deployed on its own
(`adk deploy agent_engine ./coordinator`, `gcloud run deploy --source
./formatter_agent`) and cannot import from the repo root or from each other.

## Configuration

The root `.env` is the single source of truth locally; `run.sh` exports it and
each package's `config.py` also reads it as a fallback. Real environment
variables always win, which is how Cloud Run and Kubernetes inject config without
shipping a file — `.env` is in both `.dockerignore` files and never enters an
image.

Keys worth knowing:

| key | effect |
|---|---|
| `ADK_MODEL` | must be a Vertex **publisher** model id. `gemini-flash-latest` is an AI Studio alias and 404s on Vertex. |
| `VERTEX_SEARCH_DATASTORE` | bare id or full path. Empty runs uploads-only. |
| `ENABLE_DATASTORE` | `auto` probes at startup, `on` trusts, `off` disables. |
| `DEFAULT_SEARCH_TOP_K` | raise to reduce search rounds; each round is a full LLM round-trip. |
| `MAX_DOCUMENT_READ_CHARS` | cap on a single `read_document`. Do not remove. |
| `OCR_CACHE` | cache OCR by file hash in `.cache/ocr/`. |

## Troubleshooting

**`bad interpreter` running anything in `adk_env/bin/`** — see above; use
`python -m`.

**Formatter shows "In-process fallback"** — the card at `FORMATTER_A2A_URL` did
not resolve. Check `.run/formatter.log`. The app works either way; this is
informational.

**`ModuleNotFoundError: No module named 'a2a'`** — install with the extras:
`pip install -r requirements.txt` (needs `google-adk[a2a]`).

**`Packages starlette and sse-starlette are required`** at formatter startup —
missing `a2a-sdk[http-server]`.

**Indexed-filings lane greyed out** — either no datastore configured, or the
probe failed. Check it directly:

```bash
curl -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     -H "x-goog-user-project: $GOOGLE_CLOUD_PROJECT" \
     "https://discoveryengine.googleapis.com/v1/projects/$GOOGLE_CLOUD_PROJECT/locations/global/collections/default_collection/dataStores/$VERTEX_SEARCH_DATASTORE"
```

**Uploads rejected as "Unrecognised file type"** — files are identified by magic
bytes, not by extension or the browser's content-type. A `.pdf` that is really
HTML is rejected on purpose.

**Answers are slow** — most of the time is the specialist's own LLM loop. Raising
`DEFAULT_SEARCH_TOP_K` trades a bigger prompt for fewer round trips and is
usually the win; a thin first pass sends the agent round the loop again.

**Model 404s on Vertex** — `ADK_MODEL` is an AI Studio alias. Use a publisher id.
