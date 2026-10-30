"""A tiny local HTTP server the Claude Code mod asks before each prompt.

Why a server: starting Python and importing the SDK takes ~0.8 s; a server that
stays up answers in about the time of the Jev call itself.

Security: binds to 127.0.0.1 only, and every request must carry the header
`X-Jev-Router: 1`. A web page in your browser can POST to localhost, but it
cannot add a custom header without a CORS preflight, which this server never
approves, so random sites can't spend your Jev credit. The Host header must name
this server too, which stops DNS rebinding (a site re-pointing its own domain at
127.0.0.1 to look same-origin). Likely secrets are redacted from prompts before
they are sent to the judge (see redact.py).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from jev_core import Judge, JudgeError, default_judge

from .policy import DEFAULT_TIER_MODELS, TIERS, route
from .redact import redact

DEFAULT_PORT = 8765
HEADER = "X-Jev-Router"
# Jev reads 64k tokens per request; prompts with huge pastes are cut to head + tail.
MAX_PROMPT_CHARS = 24_000
# Request bodies above this are refused (a prompt is clipped far below it anyway).
MAX_BODY_BYTES = 1_000_000
_SOURCE_DIRS = [Path(__file__).parent, Path(__file__).parent.parent / "jev_core"]


def code_version() -> str:
    """Hash of the router and core source, so a server running old code can be spotted."""
    digest = hashlib.sha256()
    for folder in _SOURCE_DIRS:
        for path in sorted(folder.glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


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
    safe_prompt, redacted = redact(clip(prompt))
    record["redacted"] = redacted
    try:
        if forced in TIERS:
            # The user typed !light / !standard / !heavy: no judge call.
            target = DEFAULT_TIER_MODELS[forced]
            result = {"ok": True, "tier": forced, **target, "reason": "forced by user", "signals": {},
                      "judge_ms": 0, "cached": False, "cost_usd": 0.0}
        else:
            decision, verdict = route(
                judge,
                safe_prompt,
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
    started_version = code_version()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> bytes | None:
            """Reads the whole body first (so a refusal never resets the connection).
            None, after answering 400/413, when the length is missing, negative or too big."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY_BYTES:
                self.close_connection = True
                self._send(413 if length > MAX_BODY_BYTES else 400, {"ok": False, "error": "bad Content-Length"})
                return None
            return self.rfile.read(length) if length else b""

        def _allowed(self) -> bool:
            if self.headers.get(HEADER) != "1":
                self._send(403, {"ok": False, "error": f"missing {HEADER} header"})
                return False
            port = self.server.server_address[1]
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self._send(403, {"ok": False, "error": "wrong Host header"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            if self.path != "/health":
                self._send(404, {"ok": False})
            elif self._allowed():
                current = code_version()
                self._send(200, {"ok": True, "judge": judge.name, "model": judge.model,
                                 "code_version": started_version, "stale": current != started_version})

        def do_POST(self) -> None:  # noqa: N802
            raw = self._read_body()
            if raw is None or not self._allowed():
                return
            if self.path == "/shutdown":
                self._send(200, {"ok": True})
                # shutdown() waits for serve_forever to stop, so not from this thread.
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/route":
                self._send(404, {"ok": False})
                return
            try:
                body = json.loads(raw or b"{}")
            except (ValueError, json.JSONDecodeError):
                body = None
            if not isinstance(body, dict):
                self._send(400, {"ok": False, "error": "body must be a JSON object"})
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
    print(f"jev_router listening on 127.0.0.1:{port} (judge={judge.name}, model={judge.model}, "
          f"code {code_version()})", flush=True)
    server.serve_forever()
