"""Load settings from the environment or a .env file, and build the default judge stack."""

from __future__ import annotations

import os
from pathlib import Path

from .cache import CachingJudge
from .judge import Judge, JudgeConfigError
from .log import LoggingJudge


def find_env_file(start: Path | None = None) -> Path | None:
    """Explicit JEV_TOOLKIT_ENV, else the nearest .env from `start` (default cwd) upward."""
    explicit = os.environ.get("JEV_TOOLKIT_ENV")
    if explicit:
        return Path(explicit)
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        candidate = folder / ".env"
        if candidate.is_file():
            return candidate
    return None


def load_env(path: Path | None = None) -> dict[str, str]:
    """Minimal KEY=VALUE parser. Real environment variables win over the file."""
    values: dict[str, str] = {}
    path = path or find_env_file()
    if path and path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    for key in ("TYPESAFE_API_KEY", "JEV_MODEL", "JEV_JUDGE"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def default_judge(var_dir: str | Path = "var", tag: str | None = None) -> Judge:
    """Logging(Caching(real judge)), chosen by JEV_JUDGE: "jev" (default) or "fake"."""
    env = load_env()
    kind = env.get("JEV_JUDGE", "jev")
    if kind == "fake":
        from .fake import FakeJudge

        inner: Judge = FakeJudge()
    elif kind == "jev":
        key = env.get("TYPESAFE_API_KEY")
        if not key:
            raise JudgeConfigError("TYPESAFE_API_KEY is not set (put it in .env, see .env.example)")
        from .jev import DEFAULT_MODEL, JevJudge

        inner = JevJudge(api_key=key, model=env.get("JEV_MODEL") or DEFAULT_MODEL)
    else:
        raise JudgeConfigError(f"unknown JEV_JUDGE {kind!r}; use 'jev' or 'fake'")
    var = Path(var_dir)
    return LoggingJudge(CachingJudge(inner, var / "judge-cache.sqlite"), var / "judge-log.jsonl", tag=tag)
