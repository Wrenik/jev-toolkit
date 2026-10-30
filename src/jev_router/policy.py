"""Turn Jev's answers about a prompt into a tier (light / standard / heavy).

The thresholds below are first guesses, not tuned values. Every decision is
logged with the raw probabilities so they can be tuned from real use.
"""

from __future__ import annotations

import re
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
    "coding": YesNo(
        "Does this request ask to write, change, fix or debug program code or scripts "
        "(not only to explain something or look something up)?"
    ),
    "multi_file": YesNo(
        "Would doing this well involve understanding or changing several files, "
        "components or systems that interact?"
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
    # False when the session runs a model the router doesn't manage (Fable,
    # opusplan, a 1M-context variant...): the mod then only suggests.
    manages_current: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def signals_of(v: Verdict) -> dict[str, float]:
    return {
        "complexity": round(v["complexity"].score, 3),
        "complexity_conf": round(v["complexity"].confidence, 3),
        "deep": round(v["deep"].p_yes, 3),
        "mechanical": round(v["mechanical"].p_yes, 3),
        "followup": round(v["followup"].p_yes, 3),
        "coding": round(v["coding"].p_yes, 3),
        "multi_file": round(v["multi_file"].p_yes, 3),
        "risk_high": round(v["risk"].probabilities.get("high", 0.0), 3),
    }


def wanted_tier(s: dict[str, float]) -> tuple[str, str]:
    """What this prompt needs on its own, ignoring what we were running before."""
    # 0.5 means "can't tell" to the judge, so every threshold sits clearly above it.
    coding = s.get("coding", 0.0) >= 0.6
    multi_file = s.get("multi_file", 0.0) >= 0.6
    if s["risk_high"] >= 0.5:
        return "heavy", "high risk"
    # Anthropic: Sonnet is for day-to-day coding, Opus for complex reasoning and for
    # bugs in an existing codebase where edge cases matter. Real coding work that is
    # not mechanical therefore goes up at lower thresholds than other work.
    if coding and s["mechanical"] < 0.6 and (s["complexity"] >= 1.5 or s["deep"] >= 0.55 or multi_file):
        return "heavy", "complex coding work: Opus rather than staying on Sonnet"
    if s["complexity"] >= 2.5 or s["deep"] >= 0.7:
        return "heavy", "complex or needs deep reasoning"
    if s["complexity"] < 1.0 and s["mechanical"] >= 0.6 and s["risk_high"] < 0.2:
        return "light", "small mechanical task"
    return "standard", "ordinary task"


def choose_tier(s: dict[str, float], previous_tier: str | None) -> tuple[str, str]:
    """Pure policy: signals -> (tier, reason)."""
    tier, reason = wanted_tier(s)
    if previous_tier in TIERS and TIERS.index(previous_tier) > TIERS.index(tier):
        # Never step down on a short follow-up (it rides on the work before it)...
        if s["followup"] >= 0.7 and s["complexity"] < 2.0:
            return previous_tier, "follow-up to ongoing work: keep tier"
        # ...nor when the judge is unsure.
        if s["complexity_conf"] < 0.4:
            return previous_tier, f"{reason}, but judge unsure: keep {previous_tier}"
    # Stepping UP is always allowed, follow-up or not.
    return tier, reason


def effort_for(tier: str, default: str, s: dict[str, float]) -> str:
    # Anthropic: high effort is for work where verification matters, such as fixing a
    # bug in an existing codebase. Sonnet doing real coding gets high instead of medium.
    if tier == "standard" and s.get("coding", 0.0) >= 0.6 and s["complexity"] >= 1.0:
        return "high"
    return default


# Switching model mid-session forfeits the prompt cache: the whole context is
# read again on the new model. Stepping DOWN to save money can therefore cost
# more than it saves, so it is only allowed when that loss is small.
SMALL_CONTEXT_TOKENS = 20_000
# Assumed cache lifetime (the Claude Code prompt cache is up to 1 h); after this
# much idle time the cache is cold and there is nothing left to lose.
CACHE_WARM_MS = 55 * 60 * 1000


# Above this, Haiku is never chosen: Anthropic's docs say Haiku 5.5 costs more per
# token on prompts over 100k tokens, and a smaller model's window may not fit.
LIGHT_MAX_CONTEXT_TOKENS = 100_000

# Only plain Haiku/Sonnet/Opus are managed. Anything else is left alone: Fable is
# above Opus, opusplan switches by itself, and a "[1m]" variant holds more context
# than the model it would be switched to.
_MANAGED_MODEL = re.compile(r"^(?:claude-)?(haiku|sonnet|opus)(?:-[\d.-]+)?$")
_TIER_BY_FAMILY = {target["model"]: tier for tier, target in DEFAULT_TIER_MODELS.items()}


def tier_of_model(model: str | None) -> str | None:
    match = _MANAGED_MODEL.match((model or "").strip().lower())
    return _TIER_BY_FAMILY.get(match.group(1)) if match else None


def guard_step_down(
    tier: str, reason: str, current_tier: str | None, context_tokens: int | None, idle_ms: int | None
) -> tuple[str, str]:
    """Never step down when it would throw away a warm cache of a big context."""
    if current_tier not in TIERS or TIERS.index(tier) >= TIERS.index(current_tier):
        return tier, reason
    if context_tokens is None or context_tokens < SMALL_CONTEXT_TOKENS:
        return tier, reason
    if idle_ms is not None and idle_ms > CACHE_WARM_MS:
        return tier, f"{reason} (cache is cold, so switching costs nothing extra)"
    return current_tier, (
        f"{reason}, but staying on {current_tier}: switching would forfeit a warm cache of ~{context_tokens:,} tokens"
    )


def route(
    judge: Judge,
    prompt: str,
    previous_tier: str | None = None,
    tier_models: dict[str, dict[str, str]] = DEFAULT_TIER_MODELS,
    *,
    current_model: str | None = None,
    context_tokens: int | None = None,
    idle_ms: int | None = None,
) -> tuple[Decision, Verdict]:
    """Ask the judge about `prompt` and decide. Raises JudgeError (caller fails open)."""
    verdict = judge.ask({"prompt": prompt}, QUESTIONS)
    s = signals_of(verdict)
    tier, reason = choose_tier(s, previous_tier)
    managed = tier_of_model(current_model)
    manages_current = current_model is None or managed is not None
    tier, reason = guard_step_down(tier, reason, managed or previous_tier, context_tokens, idle_ms)
    if tier == "light" and context_tokens is not None and context_tokens > LIGHT_MAX_CONTEXT_TOKENS:
        tier, reason = "standard", f"{reason}, but context is ~{context_tokens:,} tokens: no Haiku above 100k"
    if not manages_current:
        reason = f"{reason} (running {current_model}, which the router doesn't manage: suggestion only)"
    target = tier_models[tier]
    effort = effort_for(tier, target["effort"], s)
    return Decision(tier, target["model"], effort, reason, s, manages_current), verdict
