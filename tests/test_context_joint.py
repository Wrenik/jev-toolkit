import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from jev_context.advisor import QUESTIONS as CONTEXT_QUESTIONS
from jev_context.broker import Broker
from jev_context.server import make_handler
from jev_core import FakeJudge, JudgeUnavailable
from jev_router.policy import QUESTIONS as ROUTER_QUESTIONS


def run_together(broker, tool_a, tool_b):
    """Both tools ask at (nearly) the same moment from two threads; returns their results or errors."""
    results, barrier = {}, threading.Barrier(2)

    def go(tool, questions):
        barrier.wait()
        try:
            results[tool] = broker.view(tool).ask({"x": tool}, questions)
        except Exception as err:  # noqa: BLE001
            results[tool] = err

    threads = [threading.Thread(target=go, args=(tool_a, ROUTER_QUESTIONS)),
               threading.Thread(target=go, args=(tool_b, CONTEXT_QUESTIONS))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    return results


def known_partners(broker):
    broker._last_seen["router"] = broker._last_seen["context"] = broker._clock()


def test_two_tools_at_once_share_one_call():
    judge = FakeJudge()
    broker = Broker(judge, window_s=2.0)
    known_partners(broker)
    out = run_together(broker, "router", "context")
    assert len(judge.calls) == 1 and broker.joint_calls == 1 and broker.solo_calls == 0
    assert set(out["router"].answers) == set(ROUTER_QUESTIONS)
    assert set(out["context"].answers) == set(CONTEXT_QUESTIONS)


def test_lone_tool_never_waits_when_no_partner_was_seen():
    judge = FakeJudge()
    broker = Broker(judge, window_s=30.0)  # would hang the test if it waited
    broker.view("router").ask("s", ROUTER_QUESTIONS)
    assert len(judge.calls) == 1 and broker.solo_calls == 1


def test_lone_tool_waits_one_window_then_asks_alone_if_partner_was_seen():
    judge = FakeJudge()
    broker = Broker(judge, window_s=0.05)
    broker._last_seen["context"] = broker._clock()
    v = broker.view("router").ask("s", ROUTER_QUESTIONS)
    assert set(v.answers) == set(ROUTER_QUESTIONS) and broker.solo_calls == 1 and broker.joint_calls == 0


def test_partner_not_seen_for_long_is_forgotten():
    now = [1000.0]
    broker = Broker(FakeJudge(), window_s=30.0, partner_ttl_s=60, clock=lambda: now[0])
    broker._last_seen["context"] = 1000.0
    now[0] = 1500.0
    broker.view("router").ask("s", ROUTER_QUESTIONS)  # returns at once: the partner is stale
    assert broker.solo_calls == 1


def test_judge_failure_reaches_both_tools():
    broker = Broker(FakeJudge(fail_with=JudgeUnavailable("down")), window_s=2.0)
    known_partners(broker)
    out = run_together(broker, "router", "context")
    assert isinstance(out["router"], JudgeUnavailable) and isinstance(out["context"], JudgeUnavailable)


def test_solo_failure_is_the_normal_error():
    broker = Broker(FakeJudge(fail_with=JudgeUnavailable("down")))
    with pytest.raises(JudgeUnavailable):
        broker.view("router").ask("s", ROUTER_QUESTIONS)


# --- through the real HTTP handler -------------------------------------------------------------

@pytest.fixture
def server(tmp_path):
    judge = FakeJudge()
    broker = Broker(judge, window_s=2.0)
    handler = make_handler(broker, tmp_path / "router.jsonl", tmp_path / "advice.jsonl",
                           tmp_path / "log.sqlite", tmp_path / "none")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd, judge, broker
    httpd.shutdown()


def post(httpd, path, body, header=True):
    url = f"http://127.0.0.1:{httpd.server_address[1]}{path}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"X-Jev-Router": "1"} if header else {})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_http_route_and_advise_share_one_judge_call(server):
    httpd, judge, broker = server
    known_partners(broker)
    out = {}
    barrier = threading.Barrier(2)

    def call(name, path, body):
        barrier.wait()
        out[name] = post(httpd, path, body)

    threads = [
        threading.Thread(target=call, args=("route", "/route", {"prompt": "rename a variable"})),
        threading.Thread(target=call, args=("advise", "/advise", {"recent_prompts": ["keep going"], "context_tokens": 300_000})),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(15)
    assert len(judge.calls) == 1
    assert out["route"][0] == 200 and out["route"][1]["ok"] and out["route"][1]["tier"] in ("light", "standard", "heavy")
    assert out["advise"][0] == 200 and out["advise"][1]["ok"] and out["advise"][1]["action"] in ("keep", "compact_now", "clear_with_seed")


def test_http_each_endpoint_alone(server):
    httpd, judge, _ = server
    assert post(httpd, "/route", {"prompt": "hello"})[1]["ok"]
    assert post(httpd, "/advise", {"recent_prompts": ["x"], "context_tokens": 50_000})[1]["action"] == "keep"
    assert len(judge.calls) == 2


def test_http_health_reports_context_and_router_contract(server):
    httpd, _, _ = server
    req = urllib.request.Request(f"http://127.0.0.1:{httpd.server_address[1]}/health", headers={"X-Jev-Router": "1"})
    h = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert h["ok"] and h["has_context"] and "code_version" in h and h["stale"] is False


def test_http_requires_header_and_valid_body(server):
    httpd, _, _ = server
    assert post(httpd, "/advise", {"recent_prompts": [], "context_tokens": 1}, header=False)[0] == 403
    assert post(httpd, "/advise", {"context_tokens": "many"})[1]["ok"] is False
    assert post(httpd, "/search", {"query": "anything"})[1] == {"ok": True, "hits": []}
