"""LoggingJudge: one JSON line per judge call, for auditing and tuning thresholds.

By default the state itself is NOT logged, only its SHA-256 and length, so the
log can be shared for analysis without leaking prompts. Set `log_state=True`
for local debugging only.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .judge import Judge, JudgeError, State
from .types import Question, Verdict, question_to_dict


class LoggingJudge:
    def __init__(self, inner: Judge, path: str | Path, *, log_state: bool = False, tag: str | None = None):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_state = log_state
        # Which tool made the call ("router", "scanner", ...), so one log can hold several.
        self.tag = tag
        # Threads must not interleave half-written lines.
        self._lock = threading.Lock()

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        text = state if isinstance(state, str) else json.dumps(state, sort_keys=True, ensure_ascii=False)
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tag": self.tag,
            "judge": self.name,
            "state_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "state_chars": len(text),
            "questions": {k: question_to_dict(q) for k, q in questions.items()},
        }
        if self.log_state:
            record["state"] = text
        try:
            verdict = self.inner.ask(state, questions)
        except JudgeError as err:
            record["error"] = f"{type(err).__name__}: {err}"
            self._write(record)
            raise
        record.update(verdict.to_dict())
        record["cached"] = verdict.cached
        record["cost_usd"] = verdict.cost_usd
        self._write(record)
        return verdict

    def _write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line)
