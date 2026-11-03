"""Advisor: should this session keep going, compact, or clear? (docs/CONTEXT_COMPILER.md section 4)

The judge supplies what arithmetic cannot know (will the user continue? how much of the history
is still needed? is the task over?); `cost.decide` turns that and the token numbers into one
advice. Advisory only: nothing here runs /compact or /clear. If the judge is down the advice is
"keep", never an action.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jev_core import Judge, JudgeError, Pick, Question, Rate, Verdict, YesNo
from jev_core.joint import ask_together
from jev_router.redact import redact

from .cost import Closure, Decision, Policy, Situation, Weights, decide

TOOL = "context"
NEEDED_LEVELS = ["none", "a little", "some", "most", "nearly all"]
PROMPT_CHARS = 1_000
RECENT_PROMPTS = 3

QUESTIONS: dict[str, Question] = {
    "continue": YesNo("Will the user most likely keep working on this same task with the assistant within the next hour?"),
    "needed": Rate("How much of the earlier conversation is still needed for the next steps?", NEEDED_LEVELS),
    "closure": Pick(
        "What state is the current task in?",
        {"finished": "the task is done", "paused": "the task is unfinished and will be resumed",
         "switch": "the user is moving on to something unrelated"},
    ),
}


@dataclass(frozen=True)
class Advice:
    decision: Decision
    expected_turns: float
    p_continue: float | None
    load_bearing: float | None
    closure: str | None
    judge_used: bool
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_state(recent_prompts: Sequence[str]) -> dict[str, Any]:
    """The only text the judge sees: the last few user prompts, redacted and clipped."""
    clean = [redact(p[:PROMPT_CHARS])[0] for p in recent_prompts[-RECENT_PROMPTS:]]
    return {"recent_user_prompts": clean}


def expected_turns(p_continue: float, closure: str | None) -> float:
    """Table from the plan: a finished or switched task has no more turns on this context."""
    if closure in ("finished", "switch"):
        return 1.0
    if p_continue >= 0.8:
        return 10.0
    if p_continue >= 0.5:
        return 5.0
    return 2.0


def closure_of(probs: Mapping[str, float]) -> Closure | None:
    """Finished and switch both mean "this context is not needed for what comes next", so their
    probabilities add up (live check 2026-11-03: 0.67 finished + 0.30 switch was read as unsure when
    judged one by one). Unclear stays None, which never triggers a clear."""
    over = probs.get("finished", 0.0) + probs.get("switch", 0.0)
    if over >= 0.6:
        return "finished" if probs.get("finished", 0.0) >= probs.get("switch", 0.0) else "switch"
    if probs.get("paused", 0.0) >= 0.5:
        return "paused"
    return None


def interpret(verdict: Verdict, context_tokens: int, cache_warm: bool = True,
              policy: Policy = Policy(), weights: Weights = Weights()) -> Advice:
    p = verdict["continue"].p_yes
    needed = verdict["needed"]
    load = round(needed.score / (len(NEEDED_LEVELS) - 1), 3)
    closure = closure_of(verdict["closure"].probabilities)
    n = expected_turns(p, closure)
    decision = decide(Situation(context_tokens, n, cache_warm, load_bearing=load, closure=closure), policy, weights)
    return Advice(decision, n, round(p, 3), load, closure, judge_used=True)


def fail_open(context_tokens: int, reason: str, cache_warm: bool = True,
              policy: Policy = Policy(), weights: Weights = Weights()) -> Advice:
    """Judge unavailable: keep, unless the hard ceiling forces a compact (that needs no judgement)."""
    decision = decide(Situation(context_tokens, 1.0, cache_warm), policy, weights)
    if decision.action != "compact_now":
        decision = Decision("keep", f"judge unavailable ({reason}); doing nothing", decision.cost_keep,
                            decision.cost_compact, decision.cost_clear, decision.break_even_turns)
    return Advice(decision, 1.0, None, None, None, judge_used=False, note=reason)


def advise(judge: Judge, recent_prompts: Sequence[str], context_tokens: int, cache_warm: bool = True,
           policy: Policy = Policy(), weights: Weights = Weights(), log_path: Path | None = None,
           share_with: Mapping[str, tuple[Any, Mapping[str, Question]]] | None = None) -> tuple[Advice, dict[str, Verdict]]:
    """Ask the judge and advise. `share_with` adds other tools' (state, questions) to the SAME request;
    their verdicts come back as the second value so each tool can interpret its own."""
    parts: dict[str, tuple[Any, Mapping[str, Question]]] = {TOOL: (build_state(recent_prompts), QUESTIONS)}
    parts.update(share_with or {})
    try:
        verdicts = ask_together(judge, parts)
        advice = interpret(verdicts[TOOL], context_tokens, cache_warm, policy, weights)
    except JudgeError as err:
        verdicts = {}
        advice = fail_open(context_tokens, f"{type(err).__name__}", cache_warm, policy, weights)
    if log_path is not None:
        _log(log_path, advice, context_tokens, cache_warm, recent_prompts, joint=sorted(parts))
    return advice, verdicts


def _log(path: Path, advice: Advice, context_tokens: int, cache_warm: bool, prompts: Sequence[str], joint: list[str]) -> None:
    joined = "\n".join(prompts[-RECENT_PROMPTS:])
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "context_tokens": context_tokens, "cache_warm": cache_warm,
              "prompts_sha256": hashlib.sha256(joined.encode()).hexdigest(), "joint_tools": joint, **advice.to_dict()}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
