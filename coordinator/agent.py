"""The coordinator: route and gather, then hand off to a remote analyst.

    user question
        │
        ▼
    gatherer (LlmAgent)                    ← decides WHICH specialists to call
        ├─ AgentTool → market_agent        ← google_search        (isolated)
        ├─ AgentTool → financials_agent    ← VertexAiSearch       (isolated)
        └─ AgentTool → document_agent      ← uploaded-file tools
        │
        │  emits ONE labelled message of raw material, no analysis
        ▼
    formatter_agent (RemoteA2aAgent, or a local fallback)
        │
        ▼
    final answer

TWO CONSTRAINTS THIS SHAPE EXISTS TO SATISFY
--------------------------------------------

1. The gatherer holds NO built-in search tools of its own.
   `google_search` and `VertexAiSearchTool` are Gemini *built-in* grounding
   tools. Gemini rejects any request whose tool list mixes a built-in with
   anything else — "Multiple tools are supported only when they are all search
   tools" — and two different built-ins cannot coexist either. So each built-in
   gets its own sub-agent, surfaced here as an AgentTool. From the gatherer's
   point of view those are plain function-call tools: several function calls,
   zero built-ins, which Gemini allows.

2. The formatter is a SEQUENTIAL STEP, never an AgentTool.
   Wrapping it as a tool forces the gatherer's LLM to emit a function call whose
   argument is the entire gathered payload — a large multi-line string. Gemini
   2.5 responds by slipping into code-style compositional calling
   (`print(default_api.formatter_agent(request='''…'''))`), which the parser
   rejects as MALFORMED_FUNCTION_CALL, returning an empty answer. Handing off
   through the shared session sidesteps it entirely: the gatherer only ever
   emits plain text, and RemoteA2aAgent forwards that text.
"""

from __future__ import annotations

from typing import Literal

from google.adk.agents import Agent, SequentialAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.tools.agent_tool import AgentTool

from . import config
from .formatter_fallback import build_local_formatter
from .prompts import build_gatherer_instruction
from .sub_agents import (
    build_document_agent,
    build_financials_agent,
    build_market_agent,
)

FORMATTER_A2A_URL = config.FORMATTER_A2A_URL

FormatterMode = Literal["remote", "local"]
ResearchMode = Literal["normal", "deep"]
RESEARCH_MODES: tuple[ResearchMode, ...] = ("normal", "deep")

# The gatherer writes its labelled raw material here. Set unconditionally: it
# costs nothing on the remote path, it is how the local fallback receives the
# material ({gathered_material?} in its instruction), and it gives the web app a
# clean source for the "raw gathered material" panel instead of scraping events.
GATHERED_MATERIAL_KEY = "gathered_material"

# Handed back instead of corpus results while the conversation has no uploads.
# Phrased as something the analyst can pass straight through: the gatherer is
# already told to relay an unavailable-data report unchanged rather than
# substitute another source.
NO_DOCUMENTS_REFUSAL = (
    "No document is attached to this conversation, so this question cannot be "
    "answered from the user's own material. Tell the user to upload the filing "
    "they want analysed. Do not answer it from any other source."
)


class RequiresUploadedDocuments(AgentTool):
    """An AgentTool that declines while the session has no uploaded documents.

    Uploads are the source of truth for reported figures; with none attached
    there is nothing to be truthful against, so the indexed corpus must not
    quietly answer in their place.

    This is enforced here rather than in the router's instruction because two
    prompt attempts failed outright. With nothing uploaded, "what was walmart
    total revenue in fiscal 2024" reached the corpus both times — once with the
    rule stated after the routing bullets, once with it stated before them AND
    repeated inside the corpus rule itself. The router has thinking_budget=0 by
    design, and a negative conditional does not beat a positive match when
    nothing reasons about the collision. A state lookup has no such failure mode.

    The graph is unchanged, which keeps the asymmetry documented in
    build_root_agent: lanes are decided at startup, uploads stay per-session.
    The lane is still built and still visible in the trace — it just declines,
    so the guard is legible rather than silent.
    """

    async def run_async(self, *, args: dict, tool_context) -> object:
        # `uploaded_docs` is the ready manifest the web app writes into session
        # state on every turn (webapp/api.py). Empty list, or absent entirely
        # when something else drives the graph, both mean "no uploads".
        if not tool_context.state.get("uploaded_docs"):
            return {"result": NO_DOCUMENTS_REFUSAL}
        return await super().run_async(args=args, tool_context=tool_context)


def build_remote_formatter(url: str = "") -> RemoteA2aAgent:
    """Handle to the separate formatter A2A service.

    NOTE: the agent card is resolved lazily, inside the turn — constructing this
    does not check that anything is listening. Probe first with
    health.probe_formatter if you need to know before the user does.
    """
    return RemoteA2aAgent(
        name="formatter_agent",
        description=(
            "Remote analyst service. Receives the gathered raw material plus the "
            "user's question, compares and contrasts the sources, adds analytical "
            "context, and returns one cleanly formatted final answer."
        ),
        agent_card=url or config.FORMATTER_A2A_URL,
    )


def build_root_agent(
    *,
    formatter: FormatterMode = "remote",
    formatter_url: str = "",
    enable_datastore: bool | None = None,
    enable_documents: bool | None = None,
    enable_market: bool = True,
    mode: ResearchMode = "normal",
) -> SequentialAgent:
    """Assemble the coordinator from the lanes that are actually available.

    Lanes are decided HERE, at construction time, rather than being left in the
    graph to fail at call time. A specialist that is present but broken is the
    worst option: the model calls it, waits out a 403, and often still answers
    confidently from nothing. Dropping it is deterministic and instant.

    Note the asymmetry, and it is deliberate:
      * The datastore is a STARTUP capability — it changes the graph.
      * Uploaded documents are PER-SESSION — they never change the graph.
        document_agent is always present when enabled; with no uploads its tools
        simply report an empty list.

    `mode` selects how hard the specialists think and search:
      * "normal" — specialists do not think, and are told to prefer one or two
        well-chosen searches. ~13s.
      * "deep"   — specialists think, and are told to search as many times as
        they need. ~28s.

    The ROUTER never thinks in either mode: it performs a 3-way classification
    that reasoning cannot improve, and it costs 3.3s a question. The ANALYST
    always thinks in either mode — that reasoning is the compare-and-contrast
    the whole product is for, so speed is never bought from it. Thinking budgets
    are set at construction time, which is why callers build one graph per mode
    rather than toggling a live one.
    """
    deep = mode == "deep"
    if enable_datastore is None:
        # Without an explicit decision we cannot probe here (no network I/O at
        # import time), so "auto" is optimistic: build the lane if it is
        # configured. The web app overrides this with a real probe result.
        enable_datastore = bool(config.DATASTORE_PATH) and config.ENABLE_DATASTORE != "off"
    if enable_documents is None:
        enable_documents = config.ENABLE_DOCUMENTS

    tools: list[AgentTool] = []
    if enable_documents:
        tools.append(AgentTool(agent=build_document_agent(deep=deep)))
    if enable_datastore and config.DATASTORE_PATH:
        # Guarded only when uploads are a live lane. With documents disabled the
        # corpus is the only historical source there is, and gating it on an
        # upload that can never happen would switch the lane off for good.
        financials = build_financials_agent(deep=deep)
        tools.append(
            RequiresUploadedDocuments(agent=financials)
            if enable_documents
            else AgentTool(agent=financials)
        )
    if enable_market:
        tools.append(AgentTool(agent=build_market_agent(deep=deep)))

    gatherer = Agent(
        name="gatherer",
        # ADK_MODEL must be a real Vertex publisher model id. The alias
        # "gemini-flash-latest" is AI-Studio-only and 404s on Vertex — and this
        # must run on Vertex, because VertexAiSearchTool is Vertex-only.
        model=config.MODEL,
        description="Routes a question to the right specialists and gathers their raw output.",
        instruction=build_gatherer_instruction(
            enable_market=enable_market,
            enable_datastore=enable_datastore and bool(config.DATASTORE_PATH),
            enable_documents=enable_documents,
            datastore_corpus=config.DATASTORE_CORPUS,
        ),
        tools=tools,
        output_key=GATHERED_MATERIAL_KEY,
        # Routing is a classification, not a reasoning problem. Thinking here
        # costs ~3.3s per question and has not changed a routing decision in any
        # measurement, so it stays off even in deep mode.
        generate_content_config=config.thinking_config(config.THINKING_ROUTER),
    )

    analyst = (
        build_remote_formatter(formatter_url)
        if formatter == "remote"
        else build_local_formatter()
    )

    return SequentialAgent(
        name="coordinator",
        description=(
            "Routes financial research questions to the right specialists, gathers "
            "their raw output, then hands off to the analyst for the final "
            "compare-and-contrast answer."
        ),
        sub_agents=[gatherer, analyst],
    )


# Module-level instance for `adk web`, `adk deploy`, and anything else that
# expects a `root_agent` symbol. Built from configuration only — no network
# calls at import time, which would make cold starts flaky and would hang an
# offline `adk web` behind a timeout.
root_agent = build_root_agent()
