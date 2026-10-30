import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jev_core import FakeJudge, JudgeUnavailable, PickAnswer, RateAnswer, YesNoAnswer
from jev_router import choose_tier, route
from jev_router.server import HEADER, clip, make_handler


def signals(complexity=1.5, conf=0.9, deep=0.1, mechanical=0.3, followup=0.1, risk_high=0.0):
    return {"complexity": complexity, "complexity_conf": conf, "deep": deep,
            "mechanical": mechanical, "followup": followup, "risk_high": risk_high}


@pytest.mark.parametrize(
    "s, previous, tier",
    [
        (signals(complexity=0.2, mechanical=0.9), None, "light"),
        (signals(complexity=1.5), None, "standard"),
        (signals(complexity=3.0), None, "heavy"),
        (signals(deep=0.8), None, "heavy"),
        (signals(complexity=0.2, mechanical=0.9, risk_high=0.6), None, "heavy"),
        # Follow-ups keep the tier of the work they follow.
        (signals(complexity=0.1, mechanical=0.9, followup=0.9), "heavy", "heavy"),
        # A follow-up that is itself big work is judged on its own.
        (signals(complexity=3.2, followup=0.9), "light", "heavy"),
        # Unsure judge never steps down.
        (signals(complexity=0.2, mechanical=0.9, conf=0.3), "heavy", "heavy"),
        (signals(complexity=0.2, mechanical=0.9, conf=0.3), None, "light"),
    ],
)
def test_choose_tier(s, previous, tier):
    assert choose_tier(s, previous)[0] == tier


def light_responder(state, questions):
    return {
        "complexity": RateAnswer(0.1, "trivial", 0.95, {}),
        "mechanical": YesNoAnswer(0.9),
        "risk": PickAnswer("none", 0.9, {"none": 0.9, "low": 0.1, "high": 0.0}),
    }


def test_route_maps_tier_to_model():
    decision, _ = route(FakeJudge(light_responder), "rename cnt to count")
    assert (decision.tier, decision.model, decision.effort) == ("light", "haiku", "low")
    assert decision.signals["mechanical"] == 0.9


def test_clip_keeps_head_and_tail():
    text = "A" * 30_000 + "Z"
    clipped = clip(text)
    assert len(clipped) < 25_000 and clipped.startswith("A") and clipped.endswith("Z")


@pytest.fixture
def server(tmp_path):
    def start(judge):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(judge, tmp_path / "router.jsonl"))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    servers = []
    yield start
    for srv in servers:
        srv.shutdown()


def post(url, body, header=True):
    req = urllib.request.Request(url + "/route", data=json.dumps(body).encode(), method="POST")
    if header:
        req.add_header(HEADER, "1")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_server_routes_and_logs_without_prompt_text(server, tmp_path):
    url = server(FakeJudge(light_responder))
    result = post(url, {"prompt": "secret rename", "previous_tier": None, "current_model": "claude-opus-5-5"})
    assert result["ok"] and result["tier"] == "light"
    log = (tmp_path / "router.jsonl").read_text(encoding="utf-8")
    assert "secret rename" not in log and '"tier": "light"' in log


def test_server_with_full_judge_stack_across_threads(server, tmp_path):
    # Regression: the SQLite cache was created in the main thread and crashed
    # the server's request threads (ECONNRESET in the mod).
    from jev_core import CachingJudge, LoggingJudge

    judge = LoggingJudge(CachingJudge(FakeJudge(light_responder), tmp_path / "c.sqlite"), tmp_path / "j.jsonl")
    url = server(judge)
    first = post(url, {"prompt": "rename cnt"})
    second = post(url, {"prompt": "rename cnt"})
    assert first["ok"] and first["tier"] == "light"
    assert second["ok"] and second["cached"] is True


def test_server_fails_open_when_judge_down(server):
    url = server(FakeJudge(fail_with=JudgeUnavailable("offline")))
    result = post(url, {"prompt": "x"})
    assert result["ok"] is False and "JudgeUnavailable" in result["error"]


def test_server_rejects_requests_without_header(server):
    url = server(FakeJudge())
    with pytest.raises(urllib.error.HTTPError) as err:
        post(url, {"prompt": "x"}, header=False)
    assert err.value.code == 403
