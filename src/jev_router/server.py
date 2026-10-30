"""A tiny local HTTP server the Claude Code mod asks before each prompt.

Why a server: starting Python and importing the SDK takes ~0.8 s; a server that
stays up answers in about the time of the Jev call itself.

Security: binds to 127.0.0.1 only, and every request must carry the header
`X-Jev-Router: 1`. A web page in your browser can POST to localhost, but it
cannot add a custom header without a CORS preflight, which this server never
approves, so random sites can't spend your Jev credit.
"""

from __future__ import annotations

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jev_core import Judge, JudgeError, default_judge

from .policy import DEFAULT_TIER_MODELS, TIERS, route

DEFAULT_PORT = 8765
HEADER = "X-Jev-Router"
# Jev reads 64k tokens per request; prompts with huge pastes are cut to head + tail.
MAX_PROMPT_CHARS = 24_000


def clip(prompt: str) -> str:
    if len(prompt) <= MAX_PROMPT_CHARS:
        return prompt
    half = MAX_PROMPT_CHARS // 2
    return prompt[:half] + "\n[... middle omitted ...]\n" + prompt[-half:]


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def handle_route(judge: Judge, body: dict[str, Any], log_path: Path) -> dict[str, Any]:
    prompt = str(body.get("prompt", ""))
    previous = body.get("previous_tier")
    previous = previous if previous in TIERS else None
    started = time.perf_counter()
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "prompt_chars": len(prompt),
        "previous_tier": previous,
        "current_model": body.get("current_model"),
        "context_tokens": _int(body.get("context_tokens")),
        "idle_ms": _int(body.get("idle_ms")),
        "mode": body.get("mode"),
    }
    forced = body.get("forced_tier")
    try:
        if forced in TIERS:
            # The user typed !light / !standard / !heavy: no judge call.
            target = DEFAULT_TIER_MODELS[forced]
            result = {"ok": True, "tier": forced, **target, "reason": "forced by user", "signals": {},
                      "judge_ms": 0, "cached": False, "cost_usd": 0.0}
        else:
            decision, verdict = route(
                judge,
                clip(prompt),
                previous,
                current_model=body.get("current_model"),
                context_tokens=_int(body.get("context_tokens")),
                idle_ms=_int(body.get("idle_ms")),
            )
            result = {"ok": True, **decision.to_dict(), "judge_ms": verdict.latency_ms,
                      "cached": verdict.cached, "cost_usd": verdict.cost_usd}
    except JudgeError as err:
        # Fail open: the mod leaves the session as it is.
        result = {"ok": False, "error": f"{type(err).__name__}: {err}"}
    except Exception as err:  # noqa: BLE001
        # A bug here must still answer, not reset the connection.
        result = {"ok": False, "error": f"internal {type(err).__name__}: {err}"}
    result["total_ms"] = round((time.perf_counter() - started) * 1000)
    record.update(result)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return result


def make_handler(judge: Judge, log_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _allowed(self) -> bool:
            if self.headers.get(HEADER) != "1":
                self._send(403, {"ok": False, "error": f"missing {HEADER} header"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path == "/health" and self._allowed():
                self._send(200, {"ok": True, "judge": judge.name, "model": judge.model})
            elif self.path != "/health":
                self._send(404, {"ok": False})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/route":
                self._send(404, {"ok": False})
                return
            if not self._allowed():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"ok": False, "error": "bad JSON"})
                return
            self._send(200, handle_route(judge, body, log_path))

        def log_message(self, *args: Any) -> None:
            pass  # decisions go to the JSONL log, not stderr

    return Handler


def serve(port: int = DEFAULT_PORT, var_dir: str | Path = "var") -> None:
    judge = default_judge(var_dir=var_dir, tag="router")
    log_path = Path(var_dir) / "router-log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(judge, log_path))
    print(f"jev_router listening on 127.0.0.1:{port} (judge={judge.name}, model={judge.model})", flush=True)
    server.serve_forever()
