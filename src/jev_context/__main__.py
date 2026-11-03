"""Command line: python -m jev_context {ingest,search,get} ..."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from .ingest import ingest_tree
from .search import DEFAULT_BUDGET_TOKENS, get_turn, search
from .store import DEFAULT_DB, connect

DEFAULT_ROOT = Path.home() / ".claude" / "projects"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jev_context")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest", help="read transcripts into the log (idempotent)")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p = sub.add_parser("search", help="full-text search; prints short snippets")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET_TOKENS, help="max tokens of output")
    p.add_argument("--days", type=float, help="only the last N days")
    p.add_argument("--project")
    p.add_argument("--half-life", type=float, help="recency decay half-life in days")
    p = sub.add_parser("get", help="show one turn in full, with neighbours")
    p.add_argument("id")
    p.add_argument("--around", type=int, default=0)
    a = ap.parse_args(argv)
    db = connect(a.db)

    if a.cmd == "ingest":
        print(ingest_tree(db, Path(a.root)))
    elif a.cmd == "search":
        since = (datetime.now() - timedelta(days=a.days)).timestamp() if a.days else None
        for h in search(db, a.query, a.limit, a.budget, since, a.project, a.half_life):
            when = datetime.fromtimestamp(h.ts).strftime("%Y-%m-%d %H:%M") if h.ts else "?"
            print(f"{h.id}  {when}  {h.role}/{h.kind}{'/' + h.tool if h.tool else ''}\n    {h.snippet}")
    else:
        for t in get_turn(db, a.id, a.around):
            print(f"--- {t['id']} {t['role']}/{t['kind']}{' (truncated)' if t['truncated'] else ''}\n{t['text']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
