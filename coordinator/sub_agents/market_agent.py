from google.adk.agents import Agent
from google.adk.tools import google_search

from .. import config

# market_agent: current / live questions about any public company or market,
# answered from web search. Holds the google_search built-in tool as its ONLY
# tool, so Gemini's "search tools can't mix with other tools" rule is never
# violated inside this isolated sub-agent.
#
# Deliberately company-agnostic: the user may ask about whatever filing they
# just uploaded. Hardcoding one issuer here made the agent steer every answer
# back to that company.
market_agent = Agent(
    name="market_agent",
    model=config.MODEL,
    instruction=(
        "You are a financial research assistant covering public companies and the "
        "broader market. Use the google_search tool to answer current and general "
        "questions — share price, recent news, competitor moves, analyst sentiment, "
        "and market trends — for whichever company or sector the question is about.\n\n"
        "Keep answers concise and factual. Ground your response in what search returns "
        "and mention the source when relevant. Include the date or recency of the data "
        "when it matters. If you cannot find a reliable answer, say so rather than "
        "guessing."
    ),
    tools=[google_search],
)
