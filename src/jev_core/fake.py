"""FakeJudge: an offline judge for tests and for running tools without a key."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .judge import State
from .types import Answer, Pick, PickAnswer, Question, Rate, RateAnswer, Verdict, YesNo, YesNoAnswer

Responder = Callable[[State, Mapping[str, Question]], Mapping[str, Answer]]


class FakeJudge:
    """Answers from `responder`, or neutral defaults; can be told to fail."""

    name = "fake"

    def __init__(self, responder: Responder | None = None, model: str = "fake-1", fail_with: Exception | None = None):
        self.model = model
        self.responder = responder
        self.fail_with = fail_with
        self.calls: list[tuple[State, Mapping[str, Question]]] = []

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        self.calls.append((state, questions))
        if self.fail_with is not None:
            raise self.fail_with
        given = dict(self.responder(state, questions)) if self.responder else {}
        answers = {name: given.get(name) or neutral(q) for name, q in questions.items()}
        tokens = len(str(state)) // 4 + sum(len(q.instructions) for q in questions.values()) // 4
        return Verdict(answers=answers, model=self.model, input_tokens=tokens, latency_ms=0)


def neutral(q: Question) -> Answer:
    """Maximally uncertain answer: what a judge that knows nothing would say."""
    if isinstance(q, YesNo):
        return YesNoAnswer(p_yes=0.5)
    if isinstance(q, Pick):
        names = list(q.options)
        p = 1 / len(names)
        return PickAnswer(choice=names[0], confidence=p, probabilities={n: p for n in names})
    if isinstance(q, Rate):
        levels = list(q.levels)
        p = 1 / len(levels)
        mid = (len(levels) - 1) / 2
        return RateAnswer(score=mid, level=levels[round(mid)], confidence=p, probabilities={lv: p for lv in levels})
    raise TypeError(f"not a jev_core question: {q!r}")
