"""Translate ADK events into the stream the browser renders.

The trace panel is the whole point of the UI: without it a multi-agent system
looks exactly like a single chatbot that is slow. This module turns the raw
event stream into something a viewer can follow — which specialist was chosen,
what it was asked, how long it took.

ONE IMPORTANT NON-OBVIOUS RULE
------------------------------
Do NOT use `event.is_final_response()` to decide what the user-facing answer is.
It returns True for any event with no function call or response and no `partial`
flag — which means it fires once per participating agent. With a SequentialAgent
you get it for the gatherer too, and the gatherer's output is labelled raw
material, not an answer. The author is the correct discriminator: only
`formatter_agent` produces the final text.

That is also why the remote formatter and the in-process fallback are both named
`formatter_agent` — the swap stays invisible to everything here.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

# The agent whose text is the user-facing answer. Everything else is trace.
FINAL_AUTHOR = "formatter_agent"

# Tool arguments and results are previews only — the full payloads can be tens
# of kilobytes and the panel just needs enough to identify the call.
ARG_PREVIEW_CHARS = 160
RESULT_PREVIEW_CHARS = 400

# Friendlier labels than the raw agent names for the trace timeline.
AGENT_LABELS = {
    "gatherer": "Router",
    "market_agent": "Live market",
    "financials_agent": "Indexed filings",
    "document_agent": "Uploaded documents",
    "formatter_agent": "Analyst",
    "coordinator": "Coordinator",
}


def _preview(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


@dataclass
class TraceMapper:
    """Stateful mapper for one turn. Not reusable across turns."""

    mode: str = "normal"
    started_at: float = field(default_factory=time.monotonic)
    _seq: int = 0
    _last_author: str = ""
    _call_started: dict[str, float] = field(default_factory=dict)
    _seen_calls: set[str] = field(default_factory=set)
    _seen_results: set[str] = field(default_factory=set)
    _answer: list[str] = field(default_factory=list)

    # -- envelope -----------------------------------------------------------
    def _event(self, type_: str, agent: str = "", **data: Any) -> dict:
        self._seq += 1
        return {
            "seq": self._seq,
            "t": int((time.monotonic() - self.started_at) * 1000),
            "type": type_,
            "agent": agent,
            "label": AGENT_LABELS.get(agent, agent),
            "data": data,
        }

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> dict:
        return self._event("start", mode=self.mode)

    def error(self, message: str, where: str = "") -> dict:
        return self._event("error", message=message, where=where)

    def gathered(self, text: str) -> dict:
        """The gatherer's labelled raw material, for the trace disclosure.

        Read from session state rather than scraped out of the text events,
        because output_key gives us the exact string the formatter was handed.
        """
        return self._event("gathered", "gatherer", text=text)

    def done(self) -> dict:
        return self._event(
            "done",
            answer="".join(self._answer),
            elapsed_ms=int((time.monotonic() - self.started_at) * 1000),
            mode=self.mode,
        )

    @property
    def answer(self) -> str:
        return "".join(self._answer)

    # -- the mapping ---------------------------------------------------------
    def map(self, event: Any) -> Iterator[dict]:
        author = getattr(event, "author", "") or ""

        if author and author != self._last_author:
            self._last_author = author
            yield self._event("agent_start", author)

        # A tool call from the router IS the routing decision: when the tool is
        # an AgentTool, its name is the sub-agent's name.
        #
        # DEDUPLICATED BY ID. In SSE streaming mode ADK surfaces the same
        # function call on both a partial event and the final one, so a single
        # routing decision would otherwise be drawn twice in the timeline.
        for call in event.get_function_calls() or []:
            key = getattr(call, "id", None) or f"{call.name}:{_preview(call.args, 80)}"
            if key in self._seen_calls:
                continue
            self._seen_calls.add(key)
            self._call_started[key] = time.monotonic()
            args = dict(call.args or {})
            yield self._event(
                "tool_call",
                author,
                tool=call.name,
                tool_label=AGENT_LABELS.get(call.name, call.name),
                args={k: _preview(v, ARG_PREVIEW_CHARS) for k, v in args.items()},
            )

        for response in event.get_function_responses() or []:
            key = getattr(response, "id", None) or response.name
            if key in self._seen_results:
                continue
            self._seen_results.add(key)
            started = self._call_started.pop(key, None)
            latency = int((time.monotonic() - started) * 1000) if started else None
            payload = response.response
            ok = not (isinstance(payload, dict) and payload.get("error"))
            yield self._event(
                "tool_result",
                author,
                tool=response.name,
                tool_label=AGENT_LABELS.get(response.name, response.name),
                ok=ok,
                latency_ms=latency,
                preview=_preview(payload, RESULT_PREVIEW_CHARS),
            )

        text = self._text_of(event)
        if not text:
            return

        partial = bool(getattr(event, "partial", False))
        if author == FINAL_AUTHOR:
            if partial:
                self._answer.append(text)
                yield self._event("answer_delta", author, text=text)
            else:
                # Non-partial final text. Some transports (notably the remote
                # A2A formatter) deliver the whole answer in one message rather
                # than as deltas, so treat this as authoritative and replace
                # anything accumulated.
                self._answer = [text]
                yield self._event("answer_done", author, text=text)
        else:
            if partial:
                yield self._event("trace_delta", author, text=text)
            else:
                yield self._event("agent_message", author, text=text)

    @staticmethod
    def _text_of(event: Any) -> str:
        content = getattr(event, "content", None)
        if not content or not getattr(content, "parts", None):
            return ""
        return "".join(part.text or "" for part in content.parts)
