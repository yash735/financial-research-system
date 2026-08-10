"""market_agent — live market data via web search.

Holds the google_search built-in as its ONLY tool, so Gemini's "search tools
cannot mix with other tools" rule is never violated inside this isolated
sub-agent. The router reaches it through an AgentTool.

Deliberately company-agnostic: the user may ask about whatever filing they just
uploaded. An earlier version named one issuer in the instruction and the agent
steered every answer back to that company regardless of the question.
"""

from __future__ import annotations

from google.adk.agents import Agent
from google.adk.tools import google_search

from .. import config

INSTRUCTION = (
    "You are a financial research assistant covering public companies and the "
    "broader market. Use the google_search tool to answer current and general "
    "questions — share price, recent news, competitor moves, analyst sentiment, "
    "and market trends — for whichever company or sector the question is about.\n\n"
    "Keep answers concise and factual. Ground your response in what search "
    "returns and mention the source when relevant. Always state how current the "
    "data is: a share price without a timestamp is misleading. If you cannot "
    "find a reliable answer, say so rather than guessing."
)


def build_market_agent() -> Agent:
    return Agent(
        name="market_agent",
        model=config.MODEL,
        description=(
            "Answers current and live questions about public companies and "
            "markets using web search."
        ),
        instruction=INSTRUCTION,
        tools=[google_search],
    )
