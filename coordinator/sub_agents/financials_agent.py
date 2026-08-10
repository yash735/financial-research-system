"""financials_agent — retrieval over a pre-indexed corpus of annual reports.

Holds the VertexAiSearchTool built-in as its ONLY tool. Gemini rejects any tool
list that pairs a built-in grounding tool with anything else, so this specialist
stays isolated and is exposed to the router as an AgentTool.

Exposed as a BUILDER rather than a module-level instance because this lane is
optional: constructing VertexAiSearchTool without a datastore is meaningless, so
the coordinator only calls this when one is configured and reachable.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.tools import VertexAiSearchTool

from .. import config

# Two things worth knowing about the datastore this is designed against:
#
#   1. Give it a DETERMINISTIC id. Console-generated ids carry a random suffix,
#      so every rebuild would otherwise force a configuration change.
#   2. It must index the Document AI OCR *text*, not the source PDFs. An earlier
#      version of this project pointed the datastore at a bucket root holding
#      the raw PDFs, so retrieval silently ran on Vertex AI Search's own parser
#      while the OCR output sat unused in a subfolder. See docs/data-pipeline.md.

INSTRUCTION = (
    "You are a financial analyst working from a pre-indexed corpus of annual "
    "reports and 10-K filings, retrieved through the Vertex AI Search tool.\n\n"
    "What you can answer: revenue, net income, gross and operating margins, "
    "segment performance, and year-over-year changes — for whichever companies "
    "and fiscal years the corpus covers.\n\n"
    "Rules:\n"
    "1. Cite the source document and fiscal year for every figure you report. A "
    "figure without a period attached is not useful.\n"
    "2. Use ONLY the retrieved documents. Do not supplement with outside or "
    "current-market knowledge — other specialists cover that.\n"
    "3. If the answer is not in the indexed filings, say so clearly rather than "
    "guessing. State what you searched for, so the router knows the gap is real "
    "and not a phrasing problem."
)


def build_financials_agent(*, deep: bool = False) -> Agent:
    """Construct the specialist. Requires a configured datastore."""
    if not config.DATASTORE_PATH:
        raise ValueError(
            "VERTEX_SEARCH_DATASTORE is not configured — cannot build financials_agent."
        )
    budget = (
        config.THINKING_SPECIALIST_DEEP if deep else config.THINKING_SPECIALIST_NORMAL
    )
    return Agent(
        name="financials_agent",
        model=config.MODEL,
        description=(
            "Answers questions about historical financial results from a "
            "pre-indexed corpus of annual reports and 10-K filings."
        ),
        instruction=INSTRUCTION,
        tools=[VertexAiSearchTool(data_store_id=config.DATASTORE_PATH)],
        generate_content_config=config.thinking_config(budget),
    )
