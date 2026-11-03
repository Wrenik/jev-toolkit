"""SQLite + FTS5 store for the long-term log. Local only; text is redacted before it gets here."""

from __future__ import annotations

import hashlib
import sqlite3
import zlib
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.14+; older versions fall back to zlib (the codec is stored per blob).
    from compression import zstd
except ImportError:  # pragma: no cover
    zstd = None

DEFAULT_DB = Path("var") / "log" / "jev-context.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY, project TEXT, first_ts REAL, last_ts REAL, model TEXT
);
CREATE TABLE IF NOT EXISTS turn (
    rowid INTEGER PRIMARY KEY,
    id TEXT NOT NULL UNIQUE,           -- "<record uuid>:<block index>": the idempotency key
    session_id TEXT NOT NULL REFERENCES session(id),
    seq INTEGER NOT NULL,              -- file line * 1000 + block index: stable across re-ingests
    ts REAL,
    role TEXT NOT NULL,                -- user | assistant
    kind TEXT NOT NULL,                -- text | tool_use | tool_result
    tool TEXT,
    text TEXT NOT NULL,                -- redacted; long bodies are clipped to head+tail
    redactions INTEGER NOT NULL DEFAULT 0,
    blob_sha TEXT                      -- full redacted body in blob, when clipped
);
CREATE INDEX IF NOT EXISTS turn_session ON turn(session_id, seq);
CREATE TABLE IF NOT EXISTS blob (
    sha256 TEXT PRIMARY KEY, codec TEXT NOT NULL, size INTEGER NOT NULL, data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS file_state (path TEXT PRIMARY KEY, size INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS turn_fts USING fts5(
    text, tool, content='turn', content_rowid='rowid', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS turn_ai AFTER INSERT ON turn BEGIN
    INSERT INTO turn_fts(rowid, text, tool) VALUES (new.rowid, new.text, new.tool);
END;
CREATE TRIGGER IF NOT EXISTS turn_ad AFTER DELETE ON turn BEGIN
    INSERT INTO turn_fts(turn_fts, rowid, text, tool) VALUES ('delete', old.rowid, old.text, old.tool);
END;
"""


@dataclass(frozen=True)
class NewTurn:
    id: str
    session_id: str
    seq: int
    ts: float | None
    role: str
    kind: str
    text: str
    tool: str | None = None
    redactions: int = 0
    full_text: str | None = None  # set when `text` is a clipped version of it


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def pack(data: bytes) -> tuple[str, bytes]:
    if zstd is not None:
        return "zstd", zstd.compress(data, level=19)
    return "zlib", zlib.compress(data, 9)


def unpack(codec: str, data: bytes) -> bytes:
    if codec == "zstd":
        if zstd is None:  # pragma: no cover
            raise RuntimeError("blob was written with zstd; this Python has no compression.zstd")
        return zstd.decompress(data)
    return zlib.decompress(data)


def upsert_session(db: sqlite3.Connection, sid: str, project: str, ts: float | None, model: str | None) -> None:
    db.execute(
        "INSERT INTO session(id, project, first_ts, last_ts, model) VALUES (?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "first_ts=MIN(COALESCE(first_ts, excluded.first_ts), COALESCE(excluded.first_ts, first_ts)), "
        "last_ts=MAX(COALESCE(last_ts, excluded.last_ts), COALESCE(excluded.last_ts, last_ts)), "
        "model=COALESCE(excluded.model, model)",
        (sid, project, ts, ts, model),
    )


def add_turn(db: sqlite3.Connection, t: NewTurn) -> bool:
    """Insert one turn; returns False when its id is already stored (idempotent)."""
    sha = None
    if t.full_text is not None:
        raw = t.full_text.encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
    cur = db.execute(
        "INSERT OR IGNORE INTO turn(id, session_id, seq, ts, role, kind, tool, text, redactions, blob_sha) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (t.id, t.session_id, t.seq, t.ts, t.role, t.kind, t.tool, t.text, t.redactions, sha),
    )
    if cur.rowcount and sha is not None:
        codec, data = pack(raw)
        db.execute("INSERT OR IGNORE INTO blob(sha256, codec, size, data) VALUES (?,?,?,?)", (sha, codec, len(raw), data))
    return bool(cur.rowcount)


def get_blob(db: sqlite3.Connection, sha: str) -> str | None:
    row = db.execute("SELECT codec, data FROM blob WHERE sha256=?", (sha,)).fetchone()
    return unpack(row["codec"], row["data"]).decode("utf-8") if row else None
