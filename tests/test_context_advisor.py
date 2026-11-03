import json

from jev_context.cost import Policy
from jev_context.advisor import NEEDED_LEVELS, QUESTIONS, advise, build_state, closure_of
from jev_core import FakeJudge, JudgeUnavailable, PickAnswer, RateAnswer, YesNoAnswer
from jev_core.joint import ask_together
from jev_router.policy import QUESTIONS as ROUTER_QUESTIONS
from jev_router.policy import signals_of

# With the cautious default summary ratio (0.11) break-even sits near 10 turns whatever the context
# size, so the "compact" tests use a ratio inside the measured range (0.03-0.11).
SMALL_SUMMARY = Policy(summary_ratio=0.05)
FAKE_KEY = "sk-ant-" + "FAKEFAKEFAKEFAKE12345678"


def rate(level: int) -> RateAnswer:
    probs = {lv: (1.0 if i == level else 0.0) for i, lv in enumerate(NEEDED_LEVELS)}
    return RateAnswer(score=float(level), level=NEEDED_LEVELS[level], confidence=1.0, probabilities=probs)


def pick(choice: str) -> PickAnswer:
    names = list(QUESTIONS["closure"].options)
    return PickAnswer(choice=choice, confidence=0.9, probabilities={n: (0.9 if n == choice else 0.05) for n in names})


def judge_saying(p_continue=0.9, needed=3, closure="paused"):
    def responder(state, questions):
        mapping = {"continue": YesNoAnswer(p_continue), "needed": rate(needed), "closure": pick(closure)}
        return {k: mapping[k.split(".")[-1]] for k in questions if k.split(".")[-1] in mapping}
    return FakeJudge(responder)


def test_big_context_ongoing_task_compacts():
    advice, _ = advise(judge_saying(0.9, 3, "paused"), ["keep going on the parser"], 400_000, policy=SMALL_SUMMARY)
    assert advice.decision.action == "compact_now" and advice.expected_turns == 10 and advice.judge_used


def test_finished_and_not_needed_clears():
    advice, _ = advise(judge_saying(0.2, 0, "finished"), ["done, thanks"], 400_000)
    assert advice.decision.action == "clear_with_seed"


def test_finished_but_still_needed_does_not_clear():
    advice, _ = advise(judge_saying(0.2, 4, "finished"), ["done, thanks"], 400_000)
    assert advice.decision.action != "clear_with_seed"


def test_small_context_keeps():
    assert advise(judge_saying(), ["x"], 40_000)[0].decision.action == "keep"


def test_judge_down_fails_open():
    judge = FakeJudge(fail_with=JudgeUnavailable("down"))
    advice, verdicts = advise(judge, ["x"], 400_000)
    assert advice.decision.action == "keep" and not advice.judge_used and verdicts == {}
    # ... except that the hard ceiling needs no judgement
    assert advise(judge, ["x"], 750_000)[0].decision.action == "compact_now"


def test_state_is_redacted_and_limited():
    state = build_state([f"old {i}" for i in range(10)] + [f"use {FAKE_KEY} now"])
    text = json.dumps(state)
    assert FAKE_KEY not in text and "[REDACTED" in text
    assert len(state["recent_user_prompts"]) == 3


def test_decisions_are_logged_without_prompt_text(tmp_path):
    log = tmp_path / "sub" / "advice.jsonl"
    advise(judge_saying(), ["a secret plan about foxes"], 400_000, policy=SMALL_SUMMARY, log_path=log)
    line = log.read_text(encoding="utf-8")
    assert "foxes" not in line and json.loads(line)["decision"]["action"] == "compact_now"


def test_joint_request_is_one_call_and_each_tool_gets_its_own_answers():
    def responder(state, questions):
        out = {}
        for key in questions:
            tool, _, name = key.partition(".")
            if tool == "context":
                out[key] = {"continue": YesNoAnswer(0.9), "needed": rate(3), "closure": pick("paused")}[name]
        return out

    judge = FakeJudge(responder)
    router_part = ({"prompt": "fix the parser"}, ROUTER_QUESTIONS)
    advice, verdicts = advise(judge, ["fix the parser"], 400_000, policy=SMALL_SUMMARY, share_with={"router": router_part})
    assert len(judge.calls) == 1  # one request to the judge for both tools
    state, questions = judge.calls[0]
    assert set(questions) == {f"router.{k}" for k in ROUTER_QUESTIONS} | {f"context.{k}" for k in QUESTIONS}
    assert set(state) == {"router", "context"}
    assert all(q.instructions.startswith("Considering only the part of the input named") for q in questions.values())
    # The router's own code can read its half unchanged.
    signals = signals_of(verdicts["router"])
    assert set(signals) >= {"complexity", "deep", "risk_high"}
    assert advice.decision.action == "compact_now"
    assert verdicts["router"].input_tokens > 0 and verdicts["context"].input_tokens == 0 or \
        verdicts["context"].input_tokens > 0 and verdicts["router"].input_tokens == 0
    assert verdicts["router"].extra["joint_with"] == ["context"]


def test_alone_nothing_changes():
    judge = judge_saying()
    advise(judge, ["x"], 100_000)
    state, questions = judge.calls[0]
    assert set(questions) == set(QUESTIONS)  # no prefixes, no section wording
    assert not any("Considering only" in q.instructions for q in questions.values())


def test_same_state_parts_share_it_without_sections():
    judge = FakeJudge()
    ask_together(judge, {"a": ("same", {"q": QUESTIONS["continue"]}), "b": ("same", {"q": QUESTIONS["continue"]})})
    state, questions = judge.calls[0]
    assert state == "same" and set(questions) == {"a.q", "b.q"}
    assert all("Considering only" not in q.instructions for q in questions.values())


def test_joint_judge_failure_raises_for_the_caller_of_ask_together():
    judge = FakeJudge(fail_with=JudgeUnavailable("down"))
    try:
        ask_together(judge, {"a": ("s", {"q": QUESTIONS["continue"]}), "b": ("t", {"q": QUESTIONS["continue"]})})
        raise AssertionError("should raise")
    except JudgeUnavailable:
        pass


def test_closure_adds_finished_and_switch():
    assert closure_of({"finished": 0.67, "paused": 0.03, "switch": 0.30}) == "finished"
    assert closure_of({"finished": 0.1, "paused": 0.1, "switch": 0.8}) == "switch"
    assert closure_of({"finished": 0.1, "paused": 0.8, "switch": 0.1}) == "paused"
    assert closure_of({"finished": 0.2, "paused": 0.45, "switch": 0.35}) is None
