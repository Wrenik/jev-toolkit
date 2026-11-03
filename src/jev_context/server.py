"""One local server for both Claude Code mods (jev-router and jev-context), on the router's port.

It reuses the router's request handler unchanged for /route, /health and /shutdown, and adds /advise,
/search and /ingest. Router questions and advisor questions go through a `Broker`, so when both mods
ask at the same moment they share ONE judge request; when only one mod is installed it behaves exactly
as that mod alone would. Safety model is the router's: 127.0.0.1 only, the `X-Jev-Router: 1` header and
a matching Host header are required on every request.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jev_core import Judge, default_judge
from jev_router import server as router_server

from .advisor import advise
from .broker import Broker
from .ingest import ingest_tree
from .search import search
from .store import connect

DEFAULT_PORT = router_server.DEFAULT_PORT
MAX_PROMPTS = 5
# The main conversation's prompt cache lives one hour after its last call (Anthropic docs, see
# docs/CONTEXT_COMPILER.md); a few minutes of margin so "warm" is not claimed at the very edge.
CACHE_TTL_MS = 55 * 60 * 1000
CONTEXT_PATHS = ("/advise", "/search", "/ingest")


def _int(v: Any) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def handle_advise(judge: Judge, body: dict[str, Any], log_path: Path) -> dict[str, Any]:
    prompts = body.get("recent_prompts")
    tokens = _int(body.get("context_tokens"))
    if not isinstance(prompts, list) or tokens is None:
        return {"ok": False, "error": "need recent_prompts (list) and context_tokens (int)"}
    idle = _int(body.get("idle_ms"))
    advice, _ = advise(judge, [str(p) for p in prompts[-MAX_PROMPTS:]], tokens,
                       cache_warm=idle is None or idle < CACHE_TTL_MS, log_path=log_path)
    d = advice.decision
    return {"ok": True, "action": d.action, "reason": d.reason, "judge_used": advice.judge_used,
            "expected_turns": advice.expected_turns, "break_even_turns": d.break_even_turns,
            "load_bearing": advice.load_bearing, "closure": advice.closure,
            "cost_keep": round(d.cost_keep), "cost_compact": round(d.cost_compact), "cost_clear": round(d.cost_clear)}


def handle_search(db_path: Path, body: dict[str, Any]) -> dict[str, Any]:
    limit = min(_int(body.get("limit")) or 5, 20)
    db = connect(db_path)
    try:
        hits = search(db, str(body.get("query", "")), limit=limit)
    finally:
        db.close()
    return {"ok": True, "hits": [{"id": h.id, "ts": h.ts, "role": h.role, "kind": h.kind, "snippet": h.snippet} for h in hits]}


def handle_ingest(db_path: Path, root: Path) -> dict[str, Any]:
    db = connect(db_path)
    try:
        return {"ok": True, "stats": ingest_tree(db, root)}
    finally:
        db.close()


def make_handler(broker: Broker, router_log: Path, advice_log: Path, db_path: Path,
                 transcripts: Path) -> type[BaseHTTPRequestHandler]:
    Base = router_server.make_handler(broker.view("router"), router_log)
    context_judge = broker.view("context")

    class Handler(Base):  # type: ignore[valid-type, misc]
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health" and self._allowed():
                current = router_server.code_version()
                self._send(200, {"ok": True, "judge": broker.judge.name, "model": broker.judge.model,
                                 "code_version": started_version, "stale": current != started_version,
                                 "has_context": True, "joint_calls": broker.joint_calls,
                                 "solo_calls": broker.solo_calls})
            elif self.path != "/health":
                self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            if self.path not in CONTEXT_PATHS:
                return super().do_POST()
            raw = self._read_body()
            if raw is None or not self._allowed():
                return
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "error": "body must be a JSON object"})
                return
            try:
                if self.path == "/advise":
                    result = handle_advise(context_judge, body, advice_log)
                elif self.path == "/search":
                    result = handle_search(db_path, body)
                else:
                    result = handle_ingest(db_path, transcripts)
            except Exception as err:  # noqa: BLE001  a bug must still answer, not reset the connection
                result = {"ok": False, "error": f"internal {type(err).__name__}: {err}"}
            self._send(200, result)

    started_version = router_server.code_version()
    return Handler


def serve(port: int = DEFAULT_PORT, var_dir: str | Path = "var", transcripts: Path | None = None) -> None:
    var = Path(var_dir)
    judge = default_judge(var_dir=var, tag="shared")
    router_log = var / "router-log.jsonl"
    router_log.parent.mkdir(parents=True, exist_ok=True)
    broker = Broker(judge)
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(broker, router_log, var / "context-advice.jsonl", var / "log" / "jev-context.sqlite",
                     transcripts or Path.home() / ".claude" / "projects"),
    )
    print(f"jev_context+router listening on 127.0.0.1:{port} (judge={judge.name}, model={judge.model}, "
          f"code {router_server.code_version()})", flush=True)
    server.serve_forever()
