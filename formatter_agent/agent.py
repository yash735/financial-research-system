from google.adk.agents import Agent
from google.adk.a2a.utils.agent_to_a2a import to_a2a

# config loads this package's .env (and the repo-root .env as a fallback) at
# import time, because `uvicorn formatter_agent.agent:a2a_app` does NOT
# auto-load it the way `adk web` does. On Cloud Run / Kubernetes the values come
# from the service environment instead and the file is simply absent.
from . import config
from .prompt import FORMATTER_INSTRUCTION

# ---------------------------------------------------------------------------
# formatter_agent — a STANDALONE A2A service (its own process), NOT a sub-agent
# of the coordinator.
#
# Role: financial ANALYST + formatter. It receives, as its input message, the
# raw outputs the coordinator gathered from its specialists:
#   - historical filing data (financials_agent / Vertex AI Search), and/or
#   - excerpts from an uploaded document (document_agent / Document AI OCR), and/or
#   - current market data (market_agent / google_search).
# Its job is to COMPARE and CONTRAST those sources, add analytical context
# (trends, what the comparison implies), and format a clean final answer for the
# end user.
#
# It has NO tools. It works purely on the text passed to it — no search, no
# datastore, no live data. That is deliberate: it keeps this service light and
# dependency-free (see requirements.txt — no google-cloud-aiplatform, no
# discoveryengine).
# ---------------------------------------------------------------------------

# --- A2A advertised RPC location -------------------------------------------
# These values only control the RPC URL written INTO the auto-generated agent
# card (rpc_url = "{protocol}://{host}:{port}/"). They do NOT bind the server —
# uvicorn's --host/--port do the actual binding, and the two must agree or the
# card advertises an address nobody can reach. See formatter_agent/config.py.
A2A_HOST = config.A2A_HOST
A2A_PORT = config.A2A_PORT
A2A_PROTOCOL = config.A2A_PROTOCOL

# Runs on Vertex AI (GOOGLE_GENAI_USE_VERTEXAI=TRUE), same auth story as the
# coordinator — no separate API key. NOTE: the alias "gemini-flash-latest" is an
# AI Studio / Gemini-API alias that 404s on Vertex, so ADK_MODEL must be a real
# publisher model id (default "gemini-2.5-flash", matching the coordinator).
#
# The instruction is imported rather than written inline because the coordinator
# keeps a byte-identical copy for its in-process fallback formatter, and the two
# must not drift. scripts/check_prompt_sync.py enforces that.
root_agent = Agent(
    name="formatter_agent",
    model=config.MODEL,
    description=(
        "Remote financial analyst. Compares and contrasts historical filing data "
        "against current market data and uploaded documents, adds analytical "
        "context, and formats the final answer for the end user. Works only on "
        "text passed to it; has no search tools of its own."
    ),
    instruction=FORMATTER_INSTRUCTION,
    # No tools. Pure analysis/formatting over the input text.
)

# ---------------------------------------------------------------------------
# Expose the agent over A2A. `a2a_app` is a Starlette application that:
#   - serves the JSON-RPC A2A endpoint at  /
#   - auto-generates and serves the agent card at  /.well-known/agent-card.json
#
# Serve it with uvicorn (run from the PROJECT ROOT):
#
#   uvicorn formatter_agent.agent:a2a_app --host localhost --port 8001
#
# (Requires `pip install "google-adk[a2a]"` — see requirements.txt.)
# ---------------------------------------------------------------------------
a2a_app = to_a2a(
    root_agent,
    host=A2A_HOST,
    port=A2A_PORT,
    protocol=A2A_PROTOCOL,
)
