from google.adk.agents import Agent, SequentialAgent
from google.adk.agents.remote_a2a_agent import RemoteA2aAgent
from google.adk.tools.agent_tool import AgentTool

from . import config

# Specialists live INSIDE this package (coordinator/sub_agents/), so shipping
# `./coordinator` ships everything the coordinator needs. These are LOCAL
# (relative) imports — nothing here reaches outside the coordinator folder,
# which is what makes the package deployable to Agent Engine as-is.
from .sub_agents import market_agent          # google_search
from .sub_agents import financials_agent      # VertexAiSearch

# ============================================================================
# FORMATTER A2A URL — where the remote analyst service lives.
#
# The final analysis/formatting happens in a SEPARATE A2A service
# (formatter_agent), reached over the network. This points at that service's
# agent-card URL, and comes from configuration so the SAME image runs anywhere:
#
#   local (default):  http://localhost:8001/.well-known/agent-card.json
#   Cloud Run:        https://<svc>-<projnum>.<region>.run.app/.well-known/agent-card.json
#   Kubernetes:       http://formatter-agent:8080/.well-known/agent-card.json
#
# The `/.well-known/agent-card.json` path is where to_a2a() publishes the
# auto-generated card. Keep it on the URL.
#
# NOTE: RemoteA2aAgent resolves this card LAZILY, inside the turn — so an
# unreachable URL does not fail at startup, it fails halfway through answering.
# The web app probes it up front (see health.probe_formatter) and swaps in an
# in-process formatter when it is down.
# ============================================================================
FORMATTER_A2A_URL = config.FORMATTER_A2A_URL

# ---------------------------------------------------------------------------
# STEP 1 — the GATHERER (router). An LlmAgent that decides which specialist(s)
# a question needs, calls them, and emits ONE final message containing their raw
# outputs, clearly labeled. It does NOT analyze or format — that's the formatter.
#
# Why the gatherer holds NO direct search tools:
#   google_search and VertexAiSearchTool are Gemini "built-in" grounding tools.
#   Gemini rejects any request whose tool list mixes a built-in with anything
#   else ("Multiple tools are supported only when they are all search tools"),
#   and two different built-ins can't coexist either. So each built-in gets its
#   own sub-agent (market_agent → google_search, financials_agent → VertexAiSearch),
#   exposed here as AgentTools. From the gatherer's view these are plain
#   function-call tools, not built-ins — two function-call tools, zero built-ins,
#   which Gemini allows.
# ---------------------------------------------------------------------------
gatherer_agent = Agent(
    name="gatherer",
    # ADK_MODEL must be a valid Vertex AI publisher model. NOTE: the alias
    # "gemini-flash-latest" is an AI Studio / Gemini-API alias that 404s on
    # Vertex — and this must run on Vertex because financials_agent's
    # VertexAiSearchTool is Vertex-only. Keep this matched to the sub-agents.
    model=config.MODEL,
    instruction=(
        "You are a ROUTER and GATHERER for Walmart-related questions. You do NOT "
        "analyze, compare, or format — a separate analyst agent does that after you. "
        "Your only job is to fetch the right raw material and lay it out.\n\n"
        "STEP 1 — ROUTE. Decide which specialist(s) the question needs:\n"
        "  - Historical financial-statement questions (FY2021-FY2025 revenue, net "
        "income, margins, segment performance, year-over-year changes) → the "
        "financials_agent tool.\n"
        "  - Current or live questions (today's stock price, recent news, market "
        "sentiment, competitor moves) → the market_agent tool.\n"
        "  - Some questions need BOTH → call both.\n\n"
        "STEP 2 — GATHER. Call the needed specialist tool(s) and collect their RAW "
        "outputs verbatim. Do not summarize, edit, analyze, or compare them.\n\n"
        "STEP 3 — LAY IT OUT. Your final message must be exactly the raw material, "
        "in labeled sections (include ONLY the sections you actually gathered):\n"
        "    [HISTORICAL FINANCIALS]\n"
        "    <raw financials_agent output, including its fiscal-year citations>\n\n"
        "    [CURRENT MARKET]\n"
        "    <raw market_agent output>\n\n"
        "    [USER QUESTION]\n"
        "    <the original user question, verbatim>\n\n"
        "Never fabricate figures. If a specialist reports that data is not available, "
        "pass that statement through unchanged. Do NOT write any analysis or a "
        "polished answer — stop after laying out the labeled raw material."
    ),
    tools=[
        AgentTool(agent=market_agent),
        AgentTool(agent=financials_agent),
    ],
)

# ---------------------------------------------------------------------------
# STEP 2 — the remote FORMATTER (analyst). A RemoteA2aAgent handle to the
# separate formatter_agent A2A service. As the second step of the sequence it
# receives the gatherer's labeled raw material (forwarded automatically over
# A2A from the shared session — NOT passed as a tool argument) and returns the
# polished compare/contrast answer, which becomes the coordinator's final output.
#
# Why a SequentialAgent instead of wrapping the formatter as a third AgentTool:
#   An AgentTool would force the gatherer's LLM to emit a function call whose
#   argument is the ENTIRE combined raw payload — a huge multi-line string. Gemini
#   2.5 handles that badly: it slips into code-style compositional calling
#   (`print(default_api.formatter_agent(request='''...'''))`) and the parser
#   rejects it as MALFORMED_FUNCTION_CALL, returning an empty answer. Handing off
#   via the session (SequentialAgent) sidesteps that entirely — the gatherer only
#   ever emits plain text, and RemoteA2aAgent forwards it.
# ---------------------------------------------------------------------------
formatter_agent = RemoteA2aAgent(
    name="formatter_agent",
    description=(
        "Remote analyst service. Receives the gathered raw outputs (historical "
        "financials and/or current market data) plus the user's question, "
        "compares/contrasts them, adds analytical context, and returns one "
        "cleanly formatted final answer."
    ),
    agent_card=FORMATTER_A2A_URL,
)

# ---------------------------------------------------------------------------
# ROOT — gather THEN format. The SequentialAgent runs the gatherer first, then
# the remote formatter, and the formatter's output is what the user sees.
# ---------------------------------------------------------------------------
root_agent = SequentialAgent(
    name="coordinator",
    description=(
        "Routes Walmart questions to the right specialists, gathers their raw "
        "outputs, then hands off to the remote formatter_agent for the final "
        "compare/contrast analysis and formatting."
    ),
    sub_agents=[gatherer_agent, formatter_agent],
)
