"""Transcript JSONL -> redacted rows in the store. Idempotent by record uuid.

Kept: user and assistant text, tool calls (name + input) and tool results.
Dropped on purpose: `api-request*` records (copies of request bodies, ~25 % of the
bytes in the author's sample), thinking blocks, attachments, bookkeeping records.
Everything is redacted with the same patterns the router uses before it is stored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from jev_router.redact import redact

from .store import NewTurn, add_turn, upsert_session

# Bodies longer than this are clipped to head + tail in `turn.text`; the full redacted body goes to `blob`.
CLIP_CHARS = 8_000
_KEEP_EACH_END = CLIP_CHARS // 2


def _ts(rec: dict) -> float | None:
    t = rec.get("timestamp")
    if not isinstance(t, str):
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, dict):
                parts.append(json.dumps(b.get("content", ""), ensure_ascii=False))
            else:
                parts.append(str(b))
        return "\n".join(parts)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def _blocks(rec: dict) -> Iterator[tuple[str, str, str | None]]:
    """(kind, raw text, tool name) per kept block of one user/assistant record."""
    content = rec.get("message", {}).get("content")
    if isinstance(content, str):
        yield "text", content, None
        return
    for b in content or []:
        if not isinstance(b, dict):
            continue
        typ = b.get("type")
        if typ == "text":
            yield "text", b.get("text", ""), None
        elif typ == "tool_use":
            yield "tool_use", json.dumps(b.get("input", {}), ensure_ascii=False), b.get("name")
        elif typ == "tool_result":
            yield "tool_result", _flatten(b.get("content")), None
        # thinking, images and anything unknown are not logged


def clip(text: str) -> str:
    if len(text) <= CLIP_CHARS:
        return text
    cut = len(text) - 2 * _KEEP_EACH_END
    return f"{text[:_KEEP_EACH_END]}\n[... {cut} characters clipped; full text in blob ...]\n{text[-_KEEP_EACH_END:]}"


def ingest_lines(db: sqlite3.Connection, lines: Iterable[str], project: str = "") -> dict[str, int]:
    stats = {"records": 0, "added": 0, "duplicates": 0, "skipped": 0, "redactions": 0}
    for line_no, line in enumerate(lines):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            stats["skipped"] += 1
            continue
        role = rec.get("type")
        sid, uuid = rec.get("sessionId"), rec.get("uuid")
        if role not in ("user", "assistant") or not sid or not uuid:
            stats["skipped"] += 1
            continue
        stats["records"] += 1
        model = rec.get("message", {}).get("model") if role == "assistant" else None
        upsert_session(db, sid, project, _ts(rec), model)
        for idx, (kind, raw, tool) in enumerate(_blocks(rec)):
            if not raw.strip():
                continue
            clean, found = redact(raw)
            stats["redactions"] += len(found)
            turn = NewTurn(
                id=f"{uuid}:{idx}", session_id=sid, seq=line_no * 1000 + idx, ts=_ts(rec), role=role, kind=kind,
                text=clip(clean), tool=tool, redactions=len(found), full_text=clean if len(clean) > CLIP_CHARS else None,
            )
            stats["added" if add_turn(db, turn) else "duplicates"] += 1
    return stats


def ingest_file(db: sqlite3.Connection, path: Path) -> dict[str, int]:
    """Ingest one transcript; a file whose size is unchanged since the last run is skipped."""
    size = path.stat().st_size
    row = db.execute("SELECT size FROM file_state WHERE path=?", (str(path),)).fetchone()
    if row and row["size"] == size:
        return {"records": 0, "added": 0, "duplicates": 0, "skipped": 0, "redactions": 0, "unchanged": 1}
    with path.open(encoding="utf-8", errors="replace") as f:
        stats = ingest_lines(db, f, project=path.parent.name)
    db.execute("INSERT INTO file_state(path, size) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size",
               (str(path), size))
    db.commit()
    return stats


def ingest_tree(db: sqlite3.Connection, root: Path) -> dict[str, int]:
    total: dict[str, int] = {}
    files = sorted(root.rglob("*.jsonl"))
    for path in files:
        for k, v in ingest_file(db, path).items():
            total[k] = total.get(k, 0) + v
    total["files"] = len(files)
    return total
