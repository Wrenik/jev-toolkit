"""JevJudge: the TypeSafe Jev implementation of the Judge interface.

This is the only module that imports the TypeSafe SDK. Replacing Jev with a
local or EU model means writing one more module like this one.

Privacy: everything passed as `state` is sent to TypeSafe's servers. Their docs
(checked 2026-10-30) do not say where data is processed. Do not send secrets.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

import typesafe_sdk as ts

from .judge import JudgeConfigError, JudgeUnavailable, State
from .types import (
    Answer,
    Pick,
    PickAnswer,
    Question,
    Rate,
    RateAnswer,
    Verdict,
    YesNo,
    YesNoAnswer,
)

DEFAULT_MODEL = "jev-1.14.0"

# Errors that mean "fix your setup", not "try again later".
_CONFIG_ERRORS = (
    ts.TypeSafeAuthenticationError,
    ts.TypeSafePermissionDeniedError,
    ts.TypeSafeBadRequestError,
    ts.TypeSafeUnprocessableEntityError,
    ts.TypeSafeNotFoundError,
)


class JevJudge:
    name = "jev"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        timeout: float = 10.0,
        max_retries: int = 1,
        transport: Any = None,
    ) -> None:
        self.model = model
        try:
            self._client = ts.TypeSafeClient(
                api_key=api_key,
                model=model,
                retry=ts.RetryPolicy(max_retries=max_retries, timeout=timeout),
                transport=transport,
            )
        except ts.TypeSafeError as err:
            # Raised before any request, e.g. a missing or malformed key.
            raise JudgeConfigError(f"could not create TypeSafe client: {err}") from err

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> JevJudge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def ask(self, state: State, questions: Mapping[str, Question]) -> Verdict:
        sdk_questions = {name: _to_sdk(q) for name, q in questions.items()}
        started = time.perf_counter()
        try:
            response = self._client.system_one(state=state, questions=sdk_questions)
        except _CONFIG_ERRORS as err:
            raise JudgeConfigError(_describe(err)) from err
        except ts.TypeSafeError as err:
            # Connection errors, timeouts, rate limits, server errors.
            raise JudgeUnavailable(_describe(err)) from err
        latency_ms = round((time.perf_counter() - started) * 1000)

        answers = {name: _from_sdk(response.answers[name], questions[name]) for name in questions}
        return Verdict(
            answers=answers,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            latency_ms=latency_ms,
            # The SDK's `request_id` property raises when the header is missing.
            extra={"request_id": response.__dict__.get("_request_id")},
        )


def _to_sdk(q: Question) -> Any:
    if isinstance(q, YesNo):
        criteria = {"true": q.yes, "false": q.no} if (q.yes or q.no) else None
        return ts.Noul(instructions=q.instructions, criteria=criteria)
    if isinstance(q, Pick):
        return ts.Choice(instructions=q.instructions, criteria=dict(q.options))
    if isinstance(q, Rate):
        return ts.Score(instructions=q.instructions, criteria=list(q.levels))
    raise TypeError(f"not a jev_core question: {q!r}")


def _from_sdk(raw: Any, q: Question) -> Answer:
    if isinstance(q, YesNo):
        return YesNoAnswer(p_yes=float(raw.noul))
    if isinstance(q, Pick):
        return PickAnswer(
            choice=raw.choice,
            confidence=float(raw.confidence),
            probabilities={k: float(v) for k, v in raw.probabilities.items()},
        )
    # Rate: Jev keys levels "0", "1", ...; translate back to the level texts.
    levels = list(q.levels)
    probs = {levels[int(k)]: float(v) for k, v in raw.probabilities.items()}
    return RateAnswer(
        score=float(raw.score),
        level=max(probs, key=probs.__getitem__),
        confidence=float(raw.confidence),
        probabilities=probs,
    )


def _describe(err: Exception) -> str:
    status = getattr(err, "status", None)
    request_id = getattr(err, "request_id", None)
    parts = [type(err).__name__]
    if status is not None:
        parts.append(f"status={status}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return " ".join(parts) + f": {err}"
