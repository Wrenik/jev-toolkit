"""Turn Jev's answers about a prompt into a tier (light / standard / heavy).

The thresholds below are first guesses, not tuned values. Every decision is
logged with the raw probabilities so they can be tuned from real use.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from jev_core import Judge, Pick, Rate, Verdict, YesNo

TIERS = ("light", "standard", "heavy")

# Tier -> what Claude Code should run. Aliases are passed to /model as typed.
DEFAULT_TIER_MODELS: dict[str, dict[str, str]] = {
    "light": {"model": "haiku", "effort": "low"},
    "standard": {"model": "sonnet", "effort": "medium"},
    "heavy": {"model": "opus", "effort": "high"},
}

COMPLEXITY_LEVELS = ["trivial", "small", "medium", "large", "very large"]

QUESTIONS = {
    "complexity": Rate(
        "How much work does this request to a coding assistant need?",
        COMPLEXITY_LEVELS,
    ),
    "deep": YesNo(
        "Does this request need careful multi-step reasoning, design decisions, "
        "or debugging a problem whose cause is unknown?"
    ),
    "mechanical": YesNo(
        "Is this a mechanical task with an obvious, well-defined result "
        "(rename, small edit, run a command, look something up)?"
    ),
    "followup": YesNo(
        "Is this a short follow-up to an ongoing task (a confirmation, a go-ahead, "
        "a small correction, or a question about what was just said) rather than new work?"
    ),
    "risk": Pick(
        "If this request is done carelessly, how bad is the damage?",
        {
            "none": "read-only or easily undone",
            "low": "small, local, easy to fix",
            "high": "can delete data, break a system, leak secrets or affect other people",
        },
    ),
}


@dataclass(frozen=True)
class Decision:
    tier: str
    model: str
    effort: str
    reason: str
    signals: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def signals_of(v: Verdict) -> dict[str, float]:
    return {
        "complexity": round(v["complexity"].score, 3),
        "complexity_conf": round(v["complexity"].confidence, 3),
        "deep": round(v["deep"].p_yes, 3),
        "mechanical": round(v["mechanical"].p_yes, 3),
        "followup": round(v["followup"].p_yes, 3),
        "risk_high": round(v["risk"].probabilities.get("high", 0.0), 3),
    }


def choose_tier(s: dict[str, float], previous_tier: str | None) -> tuple[str, str]:
    """Pure policy: signals -> (tier, reason)."""
    if previous_tier in TIERS and s["followup"] >= 0.7 and s["complexity"] < 2.0:
        return previous_tier, "follow-up to ongoing work: keep tier"
    if s["risk_high"] >= 0.5:
        return "heavy", "high risk"
    if s["complexity"] >= 2.5 or s["deep"] >= 0.7:
        return "heavy", "complex or needs deep reasoning"
    if s["complexity"] < 1.0 and s["mechanical"] >= 0.6 and s["risk_high"] < 0.2:
        tier, reason = "light", "small mechanical task"
    else:
        tier, reason = "standard", "ordinary task"
    # Unsure judge: never step down from the tier we were already on.
    if s["complexity_conf"] < 0.4 and previous_tier in TIERS and TIERS.index(previous_tier) > TIERS.index(tier):
        return previous_tier, f"{reason}, but judge unsure: keep {previous_tier}"
    return tier, reason


def route(
    judge: Judge,
    prompt: str,
    previous_tier: str | None = None,
    tier_models: dict[str, dict[str, str]] = DEFAULT_TIER_MODELS,
) -> tuple[Decision, Verdict]:
    """Ask the judge about `prompt` and decide. Raises JudgeError (caller fails open)."""
    verdict = judge.ask({"prompt": prompt}, QUESTIONS)
    s = signals_of(verdict)
    tier, reason = choose_tier(s, previous_tier)
    target = tier_models[tier]
    return Decision(tier, target["model"], target["effort"], reason, s), verdict
