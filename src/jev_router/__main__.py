"""`python -m jev_router serve [port]` or `python -m jev_router decide "<prompt>" [previous_tier]`."""

from __future__ import annotations

import json
import sys

from jev_core import JudgeError, default_judge

from .policy import route
from .server import DEFAULT_PORT, serve


def main(argv: list[str]) -> int:
    if argv[:1] == ["serve"]:
        serve(int(argv[1]) if len(argv) > 1 else DEFAULT_PORT)
        return 0
    if argv[:1] == ["decide"] and len(argv) > 1:
        try:
            decision, verdict = route(default_judge(tag="router-cli"), argv[1], argv[2] if len(argv) > 2 else None)
        except JudgeError as err:
            print(f"FAILED (would fail open): {err}")
            return 1
        print(json.dumps({**decision.to_dict(), "judge_ms": verdict.latency_ms, "cached": verdict.cached}, indent=2))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
