#!/usr/bin/env python3
"""Fail if the two copies of the analyst instruction have drifted apart.

The formatter prompt exists twice on purpose:

  formatter_agent/prompt.py   the remote A2A service
  coordinator/prompts.py      the in-process fallback used when it is unreachable

`coordinator/` and `formatter_agent/` are deployed independently — `adk deploy
agent_engine ./coordinator` and `gcloud run deploy --source ./formatter_agent`
each ship one folder and nothing else — so neither can import the other. The
duplication is the honest cost of that constraint. This script is what keeps it
honest: run it before a release, or wire it into CI.

    python scripts/check_prompt_sync.py

Exits 0 when the bodies match, 1 with a diff when they do not.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coordinator.prompts import INSTRUCTION_BODY as COORDINATOR_BODY  # noqa: E402
from formatter_agent.prompt import INSTRUCTION_BODY as SERVICE_BODY  # noqa: E402


def main() -> int:
    if COORDINATOR_BODY == SERVICE_BODY:
        print(f"OK  formatter instruction is in sync ({len(SERVICE_BODY):,} chars)")
        return 0

    print("DRIFT: the formatter instruction differs between the two packages.\n")
    diff = difflib.unified_diff(
        SERVICE_BODY.splitlines(keepends=True),
        COORDINATOR_BODY.splitlines(keepends=True),
        fromfile="formatter_agent/prompt.py::INSTRUCTION_BODY",
        tofile="coordinator/prompts.py::INSTRUCTION_BODY",
        lineterm="",
    )
    sys.stdout.writelines(diff)
    print(
        "\nFix: copy the intended version into the other file. The remote service "
        "and the local fallback must behave identically, or answers change "
        "depending on whether the formatter happens to be up."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
