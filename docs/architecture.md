# Architecture

## The shape

```
browser
   │  POST /api/documents  ──►  Document AI OCR  ──►  in-memory store + BM25 index
   │  POST /api/chat (SSE)
   ▼
coordinator  =  SequentialAgent
   │
   ├─ STEP 1  gatherer (LlmAgent)
   │     ├─ AgentTool → document_agent    · FunctionTools over uploads
   │     ├─ AgentTool → financials_agent  · VertexAiSearchTool   (optional lane)
   │     └─ AgentTool → market_agent      · google_search
   │     emits ONE labelled block of raw material — no analysis
   │
   └─ STEP 2  formatter_agent
         RemoteA2aAgent ──HTTP──► separate uvicorn process
         (falls back to an in-process agent if the card does not resolve)
```

Two stages, because routing and analysis are different jobs. A router that also
writes prose starts editing the evidence on its way past.

## Why the specialists are separate agents

`google_search` and `VertexAiSearchTool` are Gemini **built-in** grounding
tools. Gemini rejects any request whose tool list pairs a built-in with anything
else:

```
Multiple tools are supported only when they are all search tools
```

and two different built-ins cannot coexist either. So each built-in gets its own
isolated sub-agent, surfaced to the router as an `AgentTool`. From the router's
side those are ordinary function-call tools — several function calls, zero
built-ins, which is legal.

`document_agent` holds plain `FunctionTool`s, so it is unaffected by the rule and
could in principle live on the router. It is kept separate anyway: the router's
instruction says to pass raw material through verbatim, and a tool returning
20 KB of excerpts would tempt it to echo all of it; and a named node in the trace
next to the other two is what makes the routing legible.

## Why the formatter is a sequential step, not a tool

The intuitive design — wrap the remote formatter as a third `AgentTool` — fails
at runtime. It forces the router's LLM to emit a function call whose argument is
the *entire* gathered payload, a large multi-line string. Gemini 2.5 responds by
slipping into code-style compositional calling:

```python
print(default_api.formatter_agent(request='''…thousands of characters…'''))
```

which the parser rejects as **`MALFORMED_FUNCTION_CALL`**, returning an empty
answer.

Handing off through the shared session sidesteps it: the gatherer only ever
emits plain text, and `RemoteA2aAgent` forwards that text as the A2A input
message. Same flow, no oversized function call.

The corollary is a rule for the document tools too: `read_document` is capped at
`MAX_DOCUMENT_READ_CHARS`. An uncapped read pushes the whole document up through
the gatherer and straight back into the situation this design exists to avoid.

## A2A and the fallback

The formatter is a standalone service exposed with `to_a2a()`. `A2A_HOST`,
`A2A_PORT` and `A2A_PROTOCOL` do **not** bind the server — uvicorn's
`--host/--port` do that. They are written into the auto-generated agent card as
`rpc_url = "{protocol}://{host}:{port}/"`, and the coordinator dials whatever the
card says. If the two disagree, the card advertises an address nobody can reach:
the fetch succeeds and the RPC goes nowhere. That is why deploying to Cloud Run
needs a follow-up env update, and why the Kubernetes manifest sets `A2A_HOST` to
the in-cluster Service name.

`RemoteA2aAgent` resolves the card **lazily, inside the turn**. An unreachable
formatter therefore does not fail at startup — it fails after the specialists
have already run, so the user watches a half-finished answer die at the last
step. The web app probes the card once during startup and, if it is down, builds
the graph with an in-process formatter instead.

Both are named `formatter_agent`, which is load-bearing: the web layer
identifies the user-facing answer by `event.author == "formatter_agent"`, so the
swap is invisible to the trace and the streaming logic.

Because the two run in different processes and cannot import each other, the
analyst instruction genuinely exists twice — `formatter_agent/prompt.py` and
`coordinator/prompts.py`. `scripts/check_prompt_sync.py` fails if they drift.

## Retrieval: two different jobs

| | Uploaded documents | Indexed corpus |
|---|---|---|
| Ingest | Document AI OCR, in-process | Document AI OCR → GCS → Vertex AI Search |
| Retrieval | BM25 over page-anchored passages | Managed, via `VertexAiSearchTool` |
| Time to first question | ~15 s for a 97-page filing | minutes to index, then instant |
| State | memory only, dies with the process | durable |

Uploads deliberately do **not** go through Vertex AI Search. Indexing takes
minutes and needs GCS staging, which is unusable in an interactive session and
makes the headline feature depend on a write path that can fail. BM25 over five
documents indexes in about a second with no network.

The lexical choice is not a shortcut: financial questions are keyword-shaped
("operating margin", "FY2023", "Sam's Club"), and numeric and fiscal-year query
terms get an extra weight because in a filing the difference between the right
answer and a plausible wrong one is usually the year.

## Capability detection changes the graph

| | when it is decided | what happens when unavailable |
|---|---|---|
| Datastore | startup probe | `financials_agent` is **dropped from the tool list** |
| Documents | startup config | `document_agent` dropped |
| Uploads within a session | per turn | nothing changes; tools report an empty list |

A broken specialist left in the graph is the worst option: the model calls it,
waits out a 403, and often still answers confidently from nothing. Dropping it
is deterministic and instant.

When a lane is dropped, the gatherer's instruction is rebuilt without it —
**both** the routing rules and the labelled output template. Telling a router to
emit a `[HISTORICAL FILINGS]` section it has no tool to fill is an invitation to
invent one.

## Session state

Document text never enters session state; it would be serialised into every
event. Session state carries only a manifest:

```python
state_delta = {
    "uploaded_docs": [{"doc_id": ..., "filename": ..., "page_count": ...}],
    "uploaded_docs_summary": "report.pdf (97 pages)",
}
```

Every document tool validates the requested `doc_id` against that manifest before
touching the store, so a session can only reach its own uploads even though the
store is process-global.

`uploaded_docs_summary` is interpolated into the router's instruction as
`{uploaded_docs_summary?}`. **The `?` is required.** ADK applies state templating
to string instructions and a missing key raises `KeyError` mid-turn, killing the
answer. Never write a bare `{word}` in an instruction in this repo.

The gatherer sets `output_key="gathered_material"`, which gives the in-process
fallback its input via `{gathered_material?}` and gives the UI the exact text the
analyst received for the "raw gathered material" panel.

## Event mapping

`webapp/trace.py` turns ADK events into the browser stream. One rule matters:

**Do not use `event.is_final_response()` to identify the answer.** It returns
True for any event with no function call or response and no `partial` flag — so
it fires once per participating agent, including the gatherer, whose output is
labelled raw material. Discriminate on the author.

Function calls are deduplicated by id: in SSE streaming mode ADK surfaces the
same call on both a partial and the final event, so a single routing decision
would otherwise appear twice.
