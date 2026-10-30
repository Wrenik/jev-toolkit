"""`python -m jev_core check` (no network) and `python -m jev_core smoke` (one real call)."""

from __future__ import annotations

import sys

from .config import default_judge, find_env_file, load_env
from .judge import JudgeError
from .types import Pick, Rate, YesNo

SMOKE_STATE = "Rename the variable `cnt` to `count` in utils.py."
SMOKE_QUESTIONS = {
    "mechanical": YesNo("Is this a small mechanical code edit with no design decisions?"),
    "risk": Pick("What is the risk of this request?", {"none": None, "low": None, "high": "could delete data or break things"}),
    "complexity": Rate("How complex is this task?", ["trivial", "small", "medium", "large", "very large"]),
}


def main(argv: list[str]) -> int:
    command = argv[0] if argv else "check"
    if command == "check":
        env = load_env()
        print(f".env file: {find_env_file() or 'not found'}")
        print(f"judge: {env.get('JEV_JUDGE', 'jev')}  model: {env.get('JEV_MODEL', '(default)')}")
        # Never print the key itself.
        print(f"TYPESAFE_API_KEY: {'set' if env.get('TYPESAFE_API_KEY') else 'MISSING'}")
        return 0
    if command == "smoke":
        try:
            verdict = default_judge(tag="smoke").ask(SMOKE_STATE, SMOKE_QUESTIONS)
        except JudgeError as err:
            print(f"FAILED: {err}")
            return 1
        print(f"model={verdict.model} tokens={verdict.input_tokens} cost=${verdict.cost_usd:.8f} "
              f"latency={verdict.latency_ms}ms cached={verdict.cached}")
        for name, answer in verdict.answers.items():
            print(f"  {name}: {answer}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
