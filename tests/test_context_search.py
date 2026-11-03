import json

from jev_context.ingest import CLIP_CHARS, ingest_lines
from jev_context.search import fts_query, get_turn, search
from jev_context.store import connect

DAY = 86400.0


def rec(typ, uuid, content, ts, sid="s1"):
    return json.dumps({"type": typ, "uuid": uuid, "sessionId": sid, "timestamp": ts, "message": {"content": content}})


def make_db():
    db = connect(":memory:")
    ingest_lines(db, [
        rec("user", "u1", "We decided to use the ferret database for the cache layer", "2026-10-01T10:00:00Z"),
        rec("assistant", "a1", [{"type": "text", "text": "Noted: ferret database it is."}], "2026-10-01T10:01:00Z"),
        rec("user", "u2", "unrelated chat about lunch", "2026-10-01T10:02:00Z"),
        rec("user", "u3", "ferret database migration plan for march", "2026-12-01T10:00:00Z", sid="s2"),
    ], project="proj-a")
    return db


def test_finds_and_ranks():
    hits = search(make_db(), "ferret database")
    assert {h.id for h in hits} == {"u1:0", "a1:0", "u3:0"}
    assert all("ferret" in h.snippet.lower() for h in hits)


def test_operators_in_query_do_not_break_it():
    db = make_db()
    search(db, 'ferret" OR NEAR( *')  # must not raise; words are ANDed, so the result may be empty
    assert [h.id for h in search(db, 'ferret"; DROP TABLE turn; --')] == []
    assert db.execute("SELECT count(*) FROM turn").fetchone()[0] == 4
    assert search(db, '"ferret" (database)') != []
    assert search(db, "  ") == []
    assert fts_query('a"b c') == '"a" "b" "c"'


def test_filters():
    db = make_db()
    assert {h.session_id for h in search(db, "ferret", project="proj-a")} == {"s1", "s2"}
    assert search(db, "ferret", project="other") == []
    since = 1_790_000_000  # after 2026-09 but before s2 (2026-12)
    assert {h.session_id for h in search(db, "ferret", since=since + 70 * DAY)} == {"s2"}


def test_budget_caps_output_but_returns_at_least_one():
    db = connect(":memory:")
    ingest_lines(db, [rec("user", f"u{i}", "needle " + "padding " * 40, "2026-10-01T10:00:00Z") for i in range(30)])
    small = search(db, "needle", limit=30, budget_tokens=200)
    big = search(db, "needle", limit=30, budget_tokens=100_000)
    assert 1 <= len(small) < len(big) == 30
    assert len(search(db, "needle", limit=5, budget_tokens=1)) == 1


def test_recency_decay_prefers_newer():
    db = connect(":memory:")
    ingest_lines(db, [
        rec("user", "old", "needle needle needle", "2025-01-01T00:00:00Z"),
        rec("user", "new", "needle", "2026-10-01T00:00:00Z"),
    ])
    now = 1_790_000_000.0
    assert search(db, "needle", now=now)[0].id == "old:0"  # more matches wins without decay
    assert search(db, "needle", half_life_days=30, now=now)[0].id == "new:0"


def test_get_turn_with_neighbours_and_blob():
    db = connect(":memory:")
    long_body = "start-" + "y" * (CLIP_CHARS * 2) + "-end"
    ingest_lines(db, [
        rec("user", "a", "first", "2026-10-01T10:00:00Z"),
        rec("user", "b", [{"type": "tool_result", "content": long_body}], "2026-10-01T10:01:00Z"),
        rec("user", "c", "third", "2026-10-01T10:02:00Z"),
        rec("user", "d", "fourth", "2026-10-01T10:03:00Z"),
    ])
    got = get_turn(db, "b:0", around=1)
    assert [t["id"] for t in got] == ["a:0", "b:0", "c:0"]
    assert got[1]["text"].startswith("start-") and got[1]["truncated"] and len(got[1]["text"]) <= 6_000
    assert get_turn(db, "b:0", around=0, max_chars=10**9)[0]["text"] == long_body
    assert get_turn(db, "missing") == []
