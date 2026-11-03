"""Broker: lets several tools in one server process share a single judge request.

Each tool gets a `view` that looks like a normal Judge. When two tools ask within a short window of
each other, their questions go out as ONE `ask_together` call; otherwise each asks alone, exactly as it
would without the broker. A tool only waits for a partner if that partner has been seen recently, so a
tool that runs by itself never pays the waiting time.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

from jev_core import Judge, Question, Verdict
from jev_core.joint import ask_together
from jev_core.judge import State

WINDOW_S = 0.3
PARTNER_TTL_S = 600.0


@dataclass
class _Pending:
    state: State
    questions: Mapping[str, Question]
    done: threading.Event = field(default_factory=threading.Event)
    verdict: Verdict | None = None
    error: BaseException | None = None


class Broker:
    def __init__(self, judge: Judge, window_s: float = WINDOW_S, partner_ttl_s: float = PARTNER_TTL_S,
                 clock=time.monotonic):
        self.judge = judge
        self.window_s = window_s
        self.partner_ttl_s = partner_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._last_seen: dict[str, float] = {}
        self.joint_calls = 0
        self.solo_calls = 0

    def view(self, tool: str) -> BrokerJudge:
        return BrokerJudge(self, tool)

    def submit(self, tool: str, state: State, questions: Mapping[str, Question]) -> Verdict:
        mine = _Pending(state, questions)
        with self._lock:
            now = self._clock()
            self._last_seen[tool] = now
            partner = next((t for t in self._pending if t != tool), None)
            if partner is not None:
                other = self._pending.pop(partner)
                parts = {partner: (other.state, other.questions), tool: (state, questions)}
            else:
                other = None
                recent = any(t != tool and now - seen < self.partner_ttl_s for t, seen in self._last_seen.items())
                if recent:
                    self._pending[tool] = mine
        if other is not None:
            return self._run_joint(parts, partner, other, tool, mine)
        if tool in self._pending and self._pending[tool] is mine:
            mine.done.wait(self.window_s)
            with self._lock:
                if self._pending.get(tool) is mine:  # nobody came: ask alone
                    del self._pending[tool]
                    give_up = True
                else:
                    give_up = False
            if not give_up:
                mine.done.wait()  # the partner took us and is running the joint call
                return self._result(mine)
        self.solo_calls += 1
        return self.judge.ask(state, questions)

    def _run_joint(self, parts, partner: str, other: _Pending, tool: str, mine: _Pending) -> Verdict:
        try:
            verdicts = ask_together(self.judge, parts)
            other.verdict = verdicts[partner]
            mine.verdict = verdicts[tool]
            self.joint_calls += 1
        except BaseException as err:  # noqa: BLE001  both callers must hear about it, then each handles it
            other.error = mine.error = err
        finally:
            other.done.set()
        return self._result(mine)

    @staticmethod
    def _result(p: _Pending) -> Verdict:
        if p.error is not None:
            raise p.error
        assert p.verdict is not None
        return p.verdict


class BrokerJudge:
    """A Judge for one tool, backed by the broker."""

    def __init__(self, broker: Broker, tool: str):
        self.broker = broker
        self.tool = tool
        self.name = broker.judge.name
        self.model = broker.judge.model

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        return self.broker.submit(self.tool, state, questions)
