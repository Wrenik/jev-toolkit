"""CachingJudge: skip repeat calls for the same state, questions and model."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .judge import Judge, State
from .types import Question, Verdict, question_to_dict


def cache_key(judge: Judge, state: State, questions: Mapping[str, Question]) -> str:
    payload = {
        "judge": judge.name,
        "model": judge.model,
        "state": state,
        "questions": {k: question_to_dict(q) for k, q in questions.items()},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class CachingJudge:
    """Wraps a judge. `path=None` keeps the cache in memory for this process only."""

    def __init__(self, inner: Judge, path: str | Path | None = None, max_age_s: float | None = None):
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.max_age_s = max_age_s
        if path is not None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        # One connection shared by every thread (the router server is threaded),
        # so sqlite's same-thread check is off and a lock serialises access.
        self._db = sqlite3.connect(":memory:" if path is None else str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, verdict TEXT, created REAL)")

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        key = cache_key(self.inner, state, questions)
        with self._lock:
            row = self._db.execute("SELECT verdict, created FROM cache WHERE key = ?", (key,)).fetchone()
        if row and (self.max_age_s is None or time.time() - row[1] <= self.max_age_s):
            return Verdict.from_dict(json.loads(row[0]), cached=True)

        # The judge call itself runs outside the lock so slow calls don't queue up.
        verdict = self.inner.ask(state, questions)
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                (key, json.dumps(verdict.to_dict()), time.time()),
            )
        return verdict

    def close(self) -> None:
        self._db.close()
