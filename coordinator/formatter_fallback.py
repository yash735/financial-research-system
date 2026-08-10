"""In-process formatter, used when the remote A2A service is unreachable.

The distributed formatter is a real part of this system's design and the default
path. But a demo must not die because one process is down, so when the agent
card does not resolve we run the same analyst locally instead.

Two details make the swap invisible to everything downstream:

  * The agent is named "formatter_agent", identical to the remote one. The web
    app identifies the user-facing answer by `event.author == "formatter_agent"`,
    so the trace and the streaming logic need no special case.

  * It reads the gathered material from session state via
    `{gathered_material?}`, which the gatherer populates through
    `output_key="gathered_material"`. The remote agent gets the same text as its
    A2A input message. Same input, same output, different transport.
"""

from __future__ import annotations

from google.adk.agents import Agent

from . import config
from .prompts import FALLBACK_FORMATTER_INSTRUCTION


def build_local_formatter() -> Agent:
    return Agent(
        name="formatter_agent",
        model=config.MODEL,
        description=(
            "In-process financial analyst. Compares and contrasts the gathered "
            "material, adds analytical context, and formats the final answer. "
            "Has no tools — it works purely on the text it is given."
        ),
        instruction=FALLBACK_FORMATTER_INSTRUCTION,
    )
