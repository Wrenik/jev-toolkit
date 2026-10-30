"""The Judge interface every tool depends on, and its errors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from .types import Pick, PickAnswer, Question, Rate, RateAnswer, Verdict, YesNo, YesNoAnswer

State = str | Mapping[str, Any]


class JudgeError(Exception):
    """Base class. Tools decide per call site whether to fail open or closed."""


class JudgeUnavailable(JudgeError):
    """Transient: network, timeout, rate limit, server error. Retrying later may work."""


class JudgeConfigError(JudgeError):
    """Not transient: missing or invalid key, bad request. Retrying will not help."""


@runtime_checkable
class Judge(Protocol):
    # Short identifier used in logs and cache keys, e.g. "jev" or "fake".
    name: str
    # The exact model (version) answering; part of every cache key.
    model: str

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        """Answer every question about `state` in one call. Raises JudgeError."""
        ...


# Convenience helpers for one-question calls. Batch questions with `ask` when you
# can: one call with three questions costs the state's tokens once, not three times.


def yes_no(judge: Judge, state: State, instructions: str) -> YesNoAnswer:
    answer = judge.ask(state, {"q": YesNo(instructions)})["q"]
    assert isinstance(answer, YesNoAnswer)
    return answer


def pick(judge: Judge, state: State, instructions: str, options: Mapping[str, str | None]) -> PickAnswer:
    answer = judge.ask(state, {"q": Pick(instructions, options)})["q"]
    assert isinstance(answer, PickAnswer)
    return answer


def rate(judge: Judge, state: State, instructions: str, levels: list[str]) -> RateAnswer:
    answer = judge.ask(state, {"q": Rate(instructions, levels)})["q"]
    assert isinstance(answer, RateAnswer)
    return answer
