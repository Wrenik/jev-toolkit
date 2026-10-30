import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jev_core import FakeJudge, JudgeUnavailable, PickAnswer, RateAnswer, YesNoAnswer
from jev_router import choose_tier, route
from jev_router.server import HEADER, clip, make_handler


def signals(complexity=1.5, conf=0.9, deep=0.1, mechanical=0.3, followup=0.1, risk_high=0.0, coding=0.0, multi_file=0.0):
    return {"complexity": complexity, "complexity_conf": conf, "deep": deep, "mechanical": mechanical,
            "followup": followup, "risk_high": risk_high, "coding": coding, "multi_file": multi_file}


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
        # Real coding work goes to Opus at lower thresholds than other work.
        (signals(complexity=1.6, coding=0.9), None, "heavy"),
        (signals(complexity=1.0, deep=0.55, coding=0.9), None, "heavy"),
        (signals(complexity=1.0, coding=0.9, multi_file=0.8), None, "heavy"),
        (signals(complexity=1.6, coding=0.1), None, "standard"),  # same size, not coding
        (signals(complexity=1.6, coding=0.9, mechanical=0.8), None, "standard"),  # mechanical edits stay cheap
        # A follow-up may go UP; it just never goes down.
        (signals(complexity=1.8, deep=0.55, coding=0.9, followup=0.9), "standard", "heavy"),
        (signals(complexity=1.0, followup=0.9), "standard", "standard"),
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


BIG = 150_000
WARM = 60_000  # idle 1 min
COLD = 70 * 60 * 1000  # idle 70 min


def test_no_step_down_with_big_warm_context():
    decision, _ = route(FakeJudge(light_responder), "rename cnt", "standard",
                        current_model="claude-sonnet-5-5", context_tokens=BIG, idle_ms=WARM)
    assert decision.tier == "standard" and "forfeit a warm cache" in decision.reason


def test_step_down_allowed_when_cache_cold_or_context_small():
    for tokens, idle in [(60_000, COLD), (5_000, WARM), (None, None)]:
        decision, _ = route(FakeJudge(light_responder), "rename cnt", "standard",
                            current_model="claude-sonnet-5-5", context_tokens=tokens, idle_ms=idle)
        assert decision.tier == "light", (tokens, idle)


def test_step_up_always_allowed():
    def heavy(state, questions):
        return {"complexity": RateAnswer(3.5, "large", 0.9, {}), "mechanical": YesNoAnswer(0.1),
                "risk": PickAnswer("low", 0.9, {"none": 0.1, "low": 0.9, "high": 0.0})}

    decision, _ = route(FakeJudge(heavy), "design a system", "light",
                        current_model="claude-haiku-5-5", context_tokens=BIG, idle_ms=WARM)
    assert decision.tier == "heavy"


def test_effort_is_high_for_sonnet_doing_real_coding():
    def coding(state, questions):
        return {"complexity": RateAnswer(1.2, "small", 0.9, {}), "coding": YesNoAnswer(0.9),
                "mechanical": YesNoAnswer(0.2),
                "risk": PickAnswer("low", 0.9, {"none": 0.1, "low": 0.9, "high": 0.0})}

    decision, _ = route(FakeJudge(coding), "fix the flaky retry in client.py")
    assert (decision.tier, decision.model, decision.effort) == ("standard", "sonnet", "high")


@pytest.mark.parametrize("model, tier", [
    ("claude-haiku-5-5", "light"), ("claude-sonnet-5-5", "standard"), ("claude-opus-5-5", "heavy"),
    ("sonnet", "standard"), ("claude-opus-5", "heavy"),
    ("claude-fable-5-1", None), ("fable", None), ("best", None), ("opusplan", None),
    ("claude-opus-5-5[1m]", None), ("sonnet[1m]", None), ("", None), (None, None),
])
def test_tier_of_model_is_strict(model, tier):
    from jev_router.policy import tier_of_model
    assert tier_of_model(model) == tier


def test_unmanaged_model_is_suggestion_only():
    for model in ("claude-fable-5-1", "opusplan", "claude-opus-5-5[1m]"):
        decision, _ = route(FakeJudge(light_responder), "rename", None,
                            current_model=model, context_tokens=BIG, idle_ms=WARM)
        assert decision.manages_current is False, model
        assert "suggestion only" in decision.reason


def test_no_haiku_above_100k_even_with_cold_cache():
    decision, _ = route(FakeJudge(light_responder), "rename", None,
                        current_model="claude-sonnet-5-5", context_tokens=BIG, idle_ms=COLD)
    assert decision.tier == "standard" and "no Haiku above 100k" in decision.reason
    decision, _ = route(FakeJudge(light_responder), "rename", None,
                        current_model="claude-sonnet-5-5", context_tokens=60_000, idle_ms=COLD)
    assert decision.tier == "light"


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


# --- Bug report fixes (docs/BUG_REPORT_2026-10-30.md) ---

def raw_request(url, data: bytes, headers: dict):
    import socket
    from urllib.parse import urlparse

    u = urlparse(url)
    s = socket.create_connection((u.hostname, u.port), timeout=3)
    lines = [f"POST /route HTTP/1.1", f"Host: 127.0.0.1:{u.port}"] + [f"{k}: {v}" for k, v in headers.items()]
    s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode() + data)
    try:
        return s.recv(200).split(b"\r\n", 1)[0]
    finally:
        s.close()


def test_l1_wrong_host_header_is_refused(server):
    url = server(FakeJudge())
    req = urllib.request.Request(url + "/health", headers={HEADER: "1", "Host": "attacker.example"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=3)
    assert err.value.code == 403


def test_l2_non_object_body_is_a_400_not_a_crash(server):
    url = server(FakeJudge())
    req = urllib.request.Request(url + "/route", data=b'["x"]', method="POST", headers={HEADER: "1"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=3)
    assert err.value.code == 400


def test_l3_negative_content_length_answers_fast(server):
    url = server(FakeJudge())
    status = raw_request(url, b"{}", {HEADER: "1", "Content-Length": "-1"})
    assert b"400" in status


def test_oversized_body_is_refused(server):
    url = server(FakeJudge())
    status = raw_request(url, b"", {HEADER: "1", "Content-Length": str(2_000_000)})
    assert b"413" in status


def test_403_does_not_reset_the_connection(server):
    url = server(FakeJudge())
    for _ in range(20):  # was flaky: the body was left unread before answering
        with pytest.raises(urllib.error.HTTPError) as err:
            post(url, {"prompt": "x" * 5000}, header=False)
        assert err.value.code == 403


def test_m4_health_reports_code_version_and_shutdown_works(server):
    url = server(FakeJudge())
    req = urllib.request.Request(url + "/health", headers={HEADER: "1"})
    health = json.loads(urllib.request.urlopen(req, timeout=3).read())
    assert len(health["code_version"]) == 12 and health["stale"] is False
    req = urllib.request.Request(url + "/shutdown", data=b"{}", method="POST", headers={HEADER: "1"})
    assert json.loads(urllib.request.urlopen(req, timeout=3).read())["ok"] is True


def test_h2_secrets_never_reach_the_judge(server, tmp_path):
    fake = FakeJudge()
    url = server(fake)
    secret = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    post(url, {"prompt": f"use key {secret} and DB_PASSWORD=hunter2hunter2 please"})
    sent = json.dumps(fake.calls[-1][0])
    assert secret not in sent and "hunter2hunter2" not in sent
    log = (tmp_path / "router.jsonl").read_text(encoding="utf-8")
    assert '"anthropic_key"' in log and secret not in log


@pytest.mark.parametrize("text, kind", [
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----", "private_key"),
    ("ghp_" + "a" * 36, "github_token"),
    ("AKIAABCDEFGHIJKLMNOP", "aws_key_id"),
    ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123", "bearer_token"),
    ("postgres://admin:s3cretpass@db.local/app", "url_password"),
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghijklmnop", "jwt"),
    ('{"api_key": "abcdef123456"}', "assignment"),
    ("OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz", "openai_key"),
])
def test_redact_catches_common_formats(text, kind):
    from jev_router.redact import redact

    cleaned, found = redact(text)
    assert kind in found and "REDACTED" in cleaned


def test_redact_leaves_ordinary_prompts_alone():
    from jev_router.redact import redact

    for text in ["fix the token parser in lexer.py", "rename password_hint to hint", "what is a secret santa?",
                 "set timeout = 30 in config"]:
        assert redact(text) == (text, [])
