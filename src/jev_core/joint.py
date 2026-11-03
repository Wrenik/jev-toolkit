"""Ask one judge call on behalf of several tools, then hand each tool back only its own answers.

A tool that runs alone is not changed by this: with a single part the call goes through
untouched. With several parts the questions get a `tool.` prefix and, when the tools look at
different states, each question is told which section of the input it is about.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from .judge import Judge, State
from .types import Question, Verdict

SEP = "."


def _scoped(tool: str, q: Question, with_section: bool) -> Question:
    if not with_section:
        return q
    return replace(q, instructions=f"Considering only the part of the input named '{tool}': {q.instructions}")


def ask_together(judge: Judge, parts: Mapping[str, tuple[State, Mapping[str, Question]]]) -> dict[str, Verdict]:
    """One `judge.ask` for all `parts` (tool name -> (state, questions)). Raises JudgeError like `ask`.

    Each returned Verdict holds only that tool's answers under their original names. The call's token
    count is charged to the first tool; the others report 0, so logs do not count it twice.
    `extra["joint_with"]` lists the tools that shared the call.
    """
    if not parts:
        return {}
    if len(parts) == 1:
        ((tool, (state, questions)),) = parts.items()
        v = judge.ask(state, questions)
        return {tool: v}

    states = [s for s, _ in parts.values()]
    same_state = all(s == states[0] for s in states[1:])
    state: State = states[0] if same_state else {tool: s for tool, (s, _) in parts.items()}
    questions = {
        f"{tool}{SEP}{name}": _scoped(tool, q, not same_state)
        for tool, (_, qs) in parts.items()
        for name, q in qs.items()
    }
    joint = judge.ask(state, questions)
    tools = list(parts)
    out: dict[str, Verdict] = {}
    for i, tool in enumerate(tools):
        prefix = tool + SEP
        answers = {k[len(prefix):]: a for k, a in joint.answers.items() if k.startswith(prefix)}
        out[tool] = Verdict(
            answers=answers, model=joint.model, input_tokens=joint.input_tokens if i == 0 else 0,
            latency_ms=joint.latency_ms, cached=joint.cached,
            extra={**dict(joint.extra), "joint_with": [t for t in tools if t != tool]},
        )
    return out
