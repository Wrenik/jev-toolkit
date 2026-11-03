import json

from jev_context.ingest import CLIP_CHARS, ingest_file, ingest_lines
from jev_context.store import connect, get_blob

# Seeded fake secrets, built from parts so no scanner mistakes this file for a leak.
FAKE_ANTHROPIC = "sk-ant-" + "FAKEFAKEFAKEFAKE12345678"
FAKE_GITHUB = "ghp_" + "F" * 24
FAKE_PASSWORD = "hunter2hunter2"
FAKE_ENV = "DB_PASSWORD=" + FAKE_PASSWORD


def rec(typ, uuid, content, sid="s1", ts="2026-10-01T10:00:00.000Z", model=None):
    message = {"content": content}
    if model:
        message["model"] = model
    return json.dumps({"type": typ, "uuid": uuid, "sessionId": sid, "timestamp": ts, "message": message})


def transcript():
    return [
        rec("user", "u1", f"please use {FAKE_ANTHROPIC} for the call"),
        rec("assistant", "a1", [{"type": "text", "text": "Sure, reading the config."},
                                {"type": "tool_use", "name": "Bash", "input": {"command": f"echo {FAKE_GITHUB}"}}],
            model="m-1"),
        rec("user", "u2", [{"type": "tool_result", "tool_use_id": "t1", "content": f"{FAKE_ENV}\nSETTING=1"}]),
        json.dumps({"type": "api-request", "uuid": "x1", "sessionId": "s1", "body": "x" * 500}),
        json.dumps({"type": "last-prompt", "sessionId": "s1"}),
        "{not json",
        rec("assistant", "a2", [{"type": "thinking", "thinking": "private musings"}]),
    ]


def dump(db):
    return " ".join(r["text"] for r in db.execute("SELECT text FROM turn"))


def test_ingest_keeps_and_drops():
    db = connect(":memory:")
    stats = ingest_lines(db, transcript(), project="p")
    kinds = [(r["role"], r["kind"], r["tool"]) for r in db.execute("SELECT * FROM turn ORDER BY seq")]
    assert kinds == [("user", "text", None), ("assistant", "text", None), ("assistant", "tool_use", "Bash"),
                     ("user", "tool_result", None)]
    assert stats["skipped"] == 3 and stats["added"] == 4
    assert "private musings" not in dump(db)


def test_secrets_never_reach_the_database():
    db = connect(":memory:")
    ingest_lines(db, transcript())
    assert "[REDACTED" in dump(db)
    assert db.execute("SELECT SUM(redactions) FROM turn").fetchone()[0] == 3
    # Nothing stored anywhere, FTS shadow tables included, may contain a seeded secret.
    everything = "\n".join(db.iterdump())
    for secret in (FAKE_ANTHROPIC, FAKE_GITHUB, FAKE_PASSWORD):
        assert secret not in everything
    assert db.execute("SELECT count(*) FROM turn_fts WHERE turn_fts MATCH ?", (FAKE_PASSWORD,)).fetchone()[0] == 0


def test_idempotent():
    db = connect(":memory:")
    ingest_lines(db, transcript())
    again = ingest_lines(db, transcript())
    assert again["added"] == 0 and again["duplicates"] == 4
    assert db.execute("SELECT count(*) FROM turn").fetchone()[0] == 4
    assert db.execute("SELECT count(*) FROM turn_fts").fetchone()[0] == 4


def test_long_body_is_clipped_with_full_copy_in_blob():
    db = connect(":memory:")
    body = "head-" + "x" * (CLIP_CHARS * 3) + "-tail"
    ingest_lines(db, [rec("user", "u9", [{"type": "tool_result", "content": body}])])
    row = db.execute("SELECT * FROM turn").fetchone()
    assert len(row["text"]) < CLIP_CHARS + 100 and row["text"].startswith("head-") and row["text"].endswith("-tail")
    assert get_blob(db, row["blob_sha"]) == body


def test_file_ingest_skips_unchanged_and_picks_up_appends(tmp_path):
    f = tmp_path / "proj" / "s.jsonl"
    f.parent.mkdir()
    lines = transcript()
    f.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    db = connect(":memory:")
    assert ingest_file(db, f)["added"] == 3
    assert ingest_file(db, f).get("unchanged") == 1
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert ingest_file(db, f)["added"] == 1
    assert db.execute("SELECT count(*) FROM turn").fetchone()[0] == 4
    assert db.execute("SELECT project FROM session").fetchone()[0] == "proj"


def test_session_times_widen():
    db = connect(":memory:")
    ingest_lines(db, [rec("user", "a", "x", ts="2026-10-02T00:00:00Z"), rec("user", "b", "y", ts="2026-10-01T00:00:00Z")])
    s = db.execute("SELECT first_ts, last_ts FROM session").fetchone()
    assert s["first_ts"] < s["last_ts"]
