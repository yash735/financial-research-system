from google.adk.agents import Agent
from google.adk.tools import VertexAiSearchTool

from .. import config

# ---------------------------------------------------------------------------
# The datastore holding the pre-indexed annual reports comes from configuration
# (VERTEX_SEARCH_DATASTORE), not from a literal path in source.
#
# Two things worth knowing about the datastore this was built against:
#   1. It has a DETERMINISTIC id (no random suffix), so rebuilding it reuses the
#      same id and no code has to change — retiring the old "new datastore =
#      new ID, go edit this line" chore.
#   2. It indexes the Document AI OCR *text*, not the source PDFs. An earlier
#      version pointed at the bucket root and silently indexed raw PDFs, so
#      retrieval ran on Vertex AI Search's own parser while the OCR output sat
#      unused. Pointing it at the ocr_text/ subfolder was the fix.
#      See docs/data-pipeline.md.
# ---------------------------------------------------------------------------
DATASTORE_PATH = config.DATASTORE_PATH

# Grounds this agent's answers on the indexed filings.
vertex_search_tool = VertexAiSearchTool(data_store_id=DATASTORE_PATH)

# financials_agent: answers strictly from the indexed 10-K / annual statements.
# Holds the VertexAiSearchTool built-in as its ONLY tool, isolated from the
# coordinator's other tools.
financials_agent = Agent(
    name="financials_agent",
    model=config.MODEL,
    instruction=(
        "You are a Walmart financial analyst. Answer questions ONLY using the indexed "
        "Walmart 10-K / annual financial statements for fiscal years FY2021 through FY2025, "
        "retrieved via the Vertex AI Search tool.\n\n"
        "Scope of what you can answer: revenue, net income, gross and operating margins, "
        "segment performance (Walmart U.S., Walmart International, Sam's Club), and "
        "year-over-year changes.\n\n"
        "Rules:\n"
        "1. Always cite the source document and fiscal year for every figure you report.\n"
        "2. Do not use outside or current-market knowledge — only the retrieved documents.\n"
        "3. If the answer is not contained in the indexed statements, clearly say it is not "
        "available in the financial statements rather than guessing."
    ),
    tools=[vertex_search_tool],
)
