import json

import httpx2
import pytest

from jev_core import (
    CachingJudge,
    FakeJudge,
    JudgeConfigError,
    JudgeUnavailable,
    LoggingJudge,
    Pick,
    Rate,
    YesNo,
    YesNoAnswer,
    yes_no,
)
from jev_core.jev import JevJudge

QUESTIONS = {
    "bug": YesNo("Is this a bug report?"),
    "area": Pick("Which area?", {"billing": None, "account": None, "app": None}),
    "severity": Rate("How severe?", ["cosmetic", "inconvenient", "blocking"]),
}

# Shaped like the documented System One response (docs.typesafe.ai/api/system-one).
DOC_RESPONSE = {
    "model": "jev-1.14.0",
    "answers": {
        "bug": {"type": "noul", "noul": 0.97},
        "area": {"type": "choice", "choice": "app", "confidence": 0.85,
                 "probabilities": {"billing": 0.03, "account": 0.12, "app": 0.85}},
        "severity": {"type": "score", "score": 1.65, "confidence": 0.8,
                     "legend": {"0": "cosmetic", "1": "inconvenient", "2": "blocking"},
                     "probabilities": {"0": 0.05, "1": 0.25, "2": 0.7}},
    },
    "usage": {"input_tokens": 140, "output_tokens": 20},
}

FAKE_KEY = "test-key-not-real"


def jev_with(handler, **kwargs):
    return JevJudge(api_key=FAKE_KEY, transport=httpx2.MockTransport(handler), max_retries=0, **kwargs)


def test_jev_maps_documented_response():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx2.Response(200, json=DOC_RESPONSE)

    verdict = jev_with(handler).ask("The app crashes on settings.", QUESTIONS)

    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["body"]["model"] == "jev-1.14.0"
    assert seen["body"]["questions"]["severity"]["criteria"] == ["cosmetic", "inconvenient", "blocking"]
    assert verdict["bug"].p_yes == 0.97 and verdict["bug"].yes
    assert verdict["area"].choice == "app"
    assert verdict["severity"].level == "blocking"
    assert verdict["severity"].probabilities["inconvenient"] == 0.25
    assert verdict.input_tokens == 140
    assert verdict.cost_usd == pytest.approx(140 * 0.042 / 1_000_000)


@pytest.mark.parametrize(
    "status, error",
    [(401, JudgeConfigError), (400, JudgeConfigError), (429, JudgeUnavailable), (500, JudgeUnavailable)],
)
def test_jev_error_mapping(status, error):
    judge = jev_with(lambda request: httpx2.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(error):
        judge.ask("x", {"q": YesNo("?")})


def test_jev_connection_failure_is_unavailable():
    def handler(request):
        raise httpx2.ConnectError("offline", request=request)

    with pytest.raises(JudgeUnavailable):
        jev_with(handler).ask("x", {"q": YesNo("?")})


def test_cache_hits_skip_inner_and_report_zero_cost(tmp_path):
    fake = FakeJudge()
    judge = CachingJudge(fake, tmp_path / "c.sqlite")
    first = judge.ask("same", QUESTIONS)
    second = judge.ask("same", QUESTIONS)
    assert len(fake.calls) == 1
    assert not first.cached and second.cached
    assert second.cost_usd == 0
    assert second.answers == first.answers
    judge.ask("different", QUESTIONS)
    assert len(fake.calls) == 2


def test_cache_key_includes_model(tmp_path):
    path = tmp_path / "c.sqlite"
    a = FakeJudge(model="m1")
    CachingJudge(a, path).ask("s", QUESTIONS)
    b = FakeJudge(model="m2")
    CachingJudge(b, path).ask("s", QUESTIONS)
    assert len(b.calls) == 1


def test_log_hides_state_by_default_and_records_errors(tmp_path):
    log = tmp_path / "log.jsonl"
    LoggingJudge(FakeJudge(), log, tag="t").ask("secret prompt", QUESTIONS)
    with pytest.raises(JudgeUnavailable):
        LoggingJudge(FakeJudge(fail_with=JudgeUnavailable("down")), log).ask("x", QUESTIONS)

    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert "secret prompt" not in log.read_text(encoding="utf-8")
    assert lines[0]["tag"] == "t" and lines[0]["state_chars"] == len("secret prompt")
    assert lines[0]["answers"]["bug"]["type"] == "yes_no"
    assert "error" in lines[1]


def test_fake_responder_and_neutral_defaults():
    fake = FakeJudge(lambda state, qs: {"bug": YesNoAnswer(0.9)})
    verdict = fake.ask("s", QUESTIONS)
    assert verdict["bug"].p_yes == 0.9
    assert verdict["area"].confidence == pytest.approx(1 / 3)
    assert verdict["severity"].level == "inconvenient"
    assert yes_no(fake, "s", "?").p_yes == 0.5


def test_default_judge_fake_needs_no_key(tmp_path, monkeypatch):
    from jev_core import default_judge

    monkeypatch.setenv("JEV_JUDGE", "fake")
    monkeypatch.setenv("JEV_TOOLKIT_ENV", str(tmp_path / "missing.env"))
    judge = default_judge(var_dir=tmp_path)
    assert judge.ask("s", QUESTIONS)["bug"].p_yes == 0.5
    assert (tmp_path / "judge-log.jsonl").exists()


def test_default_judge_without_key_is_config_error(tmp_path, monkeypatch):
    from jev_core import default_judge

    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("JEV_JUDGE", raising=False)
    monkeypatch.setenv("JEV_TOOLKIT_ENV", str(tmp_path / "missing.env"))
    with pytest.raises(JudgeConfigError):
        default_judge(var_dir=tmp_path)


@pytest.mark.live
def test_live_smoke():
    """Run with: pytest -m live  (one real call, a fraction of a cent)."""
    from jev_core import default_judge

    verdict = default_judge(tag="live-test").ask("Rename `cnt` to `count`.", {"m": YesNo("Is this a mechanical edit?")})
    assert 0 <= verdict["m"].p_yes <= 1
