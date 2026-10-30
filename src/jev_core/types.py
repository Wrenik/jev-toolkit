"""Model-independent question and answer types.

Nothing in this module knows about TypeSafe. A local or EU judge only has to
produce these same answers for every tool built on jev_core to keep working.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Union

# Jev list price on 2026-10-30: $0.042 per million input tokens, output free.
# Used only for cost estimates in logs; check docs.typesafe.ai/models before trusting it.
USD_PER_INPUT_TOKEN = 0.042 / 1_000_000


@dataclass(frozen=True)
class YesNo:
    """A yes/no question. `yes`/`no` optionally describe what each answer means."""

    instructions: str
    yes: str | None = None
    no: str | None = None


@dataclass(frozen=True)
class Pick:
    """Pick one option. Keys are option names, values optional descriptions."""

    instructions: str
    options: Mapping[str, str | None]


@dataclass(frozen=True)
class Rate:
    """Rate on an ordered scale, lowest level first (2-10 levels)."""

    instructions: str
    levels: Sequence[str]


Question = Union[YesNo, Pick, Rate]


@dataclass(frozen=True)
class YesNoAnswer:
    p_yes: float

    @property
    def yes(self) -> bool:
        return self.p_yes >= 0.5


@dataclass(frozen=True)
class PickAnswer:
    choice: str
    confidence: float
    probabilities: Mapping[str, float]


@dataclass(frozen=True)
class RateAnswer:
    # Probability-weighted level index: 0 = first level, len(levels)-1 = last.
    score: float
    # The single most probable level.
    level: str
    confidence: float
    # Level text -> probability.
    probabilities: Mapping[str, float]


Answer = Union[YesNoAnswer, PickAnswer, RateAnswer]

_ANSWER_TYPES: dict[str, type] = {"yes_no": YesNoAnswer, "pick": PickAnswer, "rate": RateAnswer}
_TYPE_NAMES = {v: k for k, v in _ANSWER_TYPES.items()}


@dataclass(frozen=True)
class Verdict:
    """All answers from one judge call, plus what the call cost."""

    answers: Mapping[str, Answer]
    model: str
    input_tokens: int
    latency_ms: int
    cached: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return 0.0 if self.cached else self.input_tokens * USD_PER_INPUT_TOKEN

    def __getitem__(self, name: str) -> Answer:
        return self.answers[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answers": {k: {"type": _TYPE_NAMES[type(a)], **asdict(a)} for k, a in self.answers.items()},
            "model": self.model,
            "input_tokens": self.input_tokens,
            "latency_ms": self.latency_ms,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, cached: bool = False) -> Verdict:
        answers = {}
        for name, raw in data["answers"].items():
            raw = dict(raw)
            answers[name] = _ANSWER_TYPES[raw.pop("type")](**raw)
        return cls(answers, data["model"], data["input_tokens"], data["latency_ms"], cached=cached)


def question_to_dict(q: Question) -> dict[str, Any]:
    """Stable, JSON-friendly form of a question (used for cache keys and logs)."""
    if isinstance(q, YesNo):
        return {"type": "yes_no", "instructions": q.instructions, "yes": q.yes, "no": q.no}
    if isinstance(q, Pick):
        return {"type": "pick", "instructions": q.instructions, "options": dict(q.options)}
    if isinstance(q, Rate):
        return {"type": "rate", "instructions": q.instructions, "levels": list(q.levels)}
    raise TypeError(f"not a jev_core question: {q!r}")
