"""Remove likely secrets from a prompt before it leaves the machine.

Pattern-based, so it is a seatbelt, not a guarantee: it catches the common
key formats and `name = value` assignments of obvious secret names. Anything it
misses is still sent to the judge. The kinds found are logged, never the values.
"""

from __future__ import annotations

import re

# (kind, pattern). Order matters: specific formats before the generic ones.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}")),
    ("url_password", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@")),
    # KEY=value / "password": "value" for obvious secret names (env files, JSON, YAML).
    ("assignment", re.compile(
        r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|private[_-]?key|access[_-]?key)[A-Z0-9_]*)"
        r"([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED)([^\s\"',;]{6,})"
    )),
]


def redact(text: str) -> tuple[str, list[str]]:
    """Returns the text with secrets replaced by `[REDACTED:<kind>]`, and the kinds found."""
    found: list[str] = []
    for kind, pattern in _PATTERNS:
        if kind == "assignment":
            def keep_name(m: re.Match[str]) -> str:
                found.append(kind)
                return f"{m.group(1)}{m.group(2)}[REDACTED:secret]"

            text = pattern.sub(keep_name, text)
            continue

        def replace(m: re.Match[str], kind: str = kind) -> str:
            found.append(kind)
            return f"[REDACTED:{kind}]"

        text = pattern.sub(replace, text)
    return text, found
