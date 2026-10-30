"""Measure Claude Code transcripts so the cold/warm/compress thresholds come from real data.

Usage:  python -m jev_context.measure [--root DIR] [--json OUT.json]

Read-only. Transcripts are JSONL files, one record per line. What is used:
  * assistant records: message.id, message.usage (input / cache_read / cache_creation tokens), timestamp
  * system/compact_boundary records: compactMetadata.preTokens / postTokens
  * user records carrying tool_result blocks: size of tool output, duplicates by content hash
Context size of one API call = input_tokens + cache_read + cache_creation (what the model saw).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import statistics
import zlib
from collections import Counter
from datetime import datetime
from pathlib import Path

try:  # Python 3.14+
    from compression import zstd
except ImportError:  # pragma: no cover
    zstd = None

DEFAULT_ROOT = Path.home() / ".claude" / "projects"
GAPS = (300, 3600)  # seconds: 5 min (subagent cache TTL) and 1 h (main-conversation TTL on subscription)


def _ts(rec: dict) -> float | None:
    t = rec.get("timestamp")
    if not t:
        return None
    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()


def _block_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        n = 0
        for b in content:
            if isinstance(b, dict):
                n += len(b.get("text", "")) if b.get("type") == "text" else len(json.dumps(b.get("content", "")))
            else:
                n += len(str(b))
        return n
    return 0


def analyse_session(path: Path) -> dict:
    raw = path.read_bytes()
    kind_bytes: Counter = Counter()
    calls: dict[str, dict] = {}  # message id -> usage (one message is split over several records)
    compactions = []
    tool_hashes: Counter = Counter()
    tool_chars = tool_dup_chars = 0
    user_chars = assistant_chars = 0
    for line in raw.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            kind_bytes["unparseable"] += len(line)
            continue
        typ = rec.get("type")
        kind_bytes[typ] += len(line)
        if typ == "assistant":
            msg = rec.get("message", {})
            u = msg.get("usage") or {}
            ts = _ts(rec)
            if msg.get("id") and ts is not None and "input_tokens" in u:
                ctx = u["input_tokens"] + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                calls[msg["id"]] = dict(ts=ts, ctx=ctx, read=u.get("cache_read_input_tokens", 0),
                                        create=u.get("cache_creation_input_tokens", 0), out=u.get("output_tokens", 0),
                                        model=msg.get("model"), side=bool(rec.get("isSidechain")))
            assistant_chars += _block_len(msg.get("content"))
        elif typ == "user":
            content = rec.get("message", {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        s = json.dumps(b.get("content", ""), sort_keys=True)
                        tool_chars += len(s)
                        h = hashlib.sha256(s.encode()).hexdigest()
                        if tool_hashes[h] and len(s) > 200:
                            tool_dup_chars += len(s)
                        tool_hashes[h] += 1
                    elif isinstance(b, dict) and b.get("type") == "text":
                        user_chars += len(b.get("text", ""))
            else:
                user_chars += _block_len(content)
        elif typ == "system" and rec.get("subtype") == "compact_boundary":
            m = rec.get("compactMetadata", {})
            compactions.append(dict(ts=_ts(rec), pre=m.get("preTokens"), post=m.get("postTokens"), trigger=m.get("trigger")))
    ordered = sorted(calls.values(), key=lambda c: c["ts"])
    main = [c for c in ordered if not c["side"]]
    gaps = []
    for a, b in zip(main, main[1:]):
        gaps.append(dict(gap=b["ts"] - a["ts"], ctx_after=b["ctx"], create_after=b["create"], prev_ctx=a["ctx"]))
    # A "cold re-read": the call re-created most of a big context (cache miss), not just appended to it.
    cold = [g for g in gaps if g["prev_ctx"] > 20000 and g["create_after"] > 0.8 * g["ctx_after"]]
    algos = {"zlib6": lambda b: zlib.compress(b, 6), "lzma": lzma.compress}
    if zstd:
        algos["zstd3"] = lambda b: zstd.compress(b, level=3)
        algos["zstd19"] = lambda b: zstd.compress(b, level=19)
    return dict(
        msg_ids=sorted(calls), file=path.name, project=path.parent.name[-40:], bytes=len(raw), kind_bytes=dict(kind_bytes),
        n_calls=len(ordered), n_main_calls=len(main), max_ctx=max((c["ctx"] for c in ordered), default=0),
        span_h=((ordered[-1]["ts"] - ordered[0]["ts"]) / 3600) if ordered else 0,
        gaps=[g["gap"] for g in gaps], gap_over={str(t): sum(1 for g in gaps if g["gap"] > t) for t in GAPS},
        cold_rereads=[dict(gap=g["gap"], ctx=g["ctx_after"]) for g in cold],
        compactions=compactions, tool_chars=tool_chars, tool_dup_chars=tool_dup_chars,
        user_chars=user_chars, assistant_chars=assistant_chars,
        compressed_bytes={k: len(f(raw)) for k, f in algos.items()},
        ctx_series=[c["ctx"] for c in main],
    )


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))] if xs else 0


def report(sessions: list[dict]) -> str:
    out: list[str] = []
    w = out.append
    tot = sum(s["bytes"] for s in sessions)
    w(f"Sessions: {len(sessions)}  total log size: {tot/1e6:.1f} MB  main-chain API calls: {sum(s['n_main_calls'] for s in sessions)}")
    kinds: Counter = Counter()
    for s in sessions:
        kinds.update(s["kind_bytes"])
    w("\nLog bytes by record type:")
    for k, v in kinds.most_common(8):
        w(f"  {str(k):28} {v/1e6:8.2f} MB  {100*v/tot:5.1f}%")
    zk = "zstd3" if zstd else "zlib6"
    w("\nPer session:")
    w(f"  {'file':14}{'MB':>7}{'calls':>7}{'maxctx':>9}{'hours':>7}{'>5m':>5}{'>1h':>5}{'cold':>5}{'compact':>8}{zk:>7}{'lzma':>7}")
    for s in sorted(sessions, key=lambda s: -s["bytes"]):
        c = s["compressed_bytes"]
        w(f"  {s['file'][:12]:14}{s['bytes']/1e6:7.2f}{s['n_main_calls']:7}{s['max_ctx']:9}{s['span_h']:7.1f}"
          f"{s['gap_over']['300']:5}{s['gap_over']['3600']:5}{len(s['cold_rereads']):5}{len(s['compactions']):8}"
          f"{s['bytes']/max(1, c[zk]):6.1f}x{s['bytes']/max(1, c['lzma']):6.1f}x")
    gaps = [g for s in sessions for g in s["gaps"]]
    if gaps:
        w(f"\nGaps between consecutive main-chain API calls (n={len(gaps)}): median {statistics.median(gaps):.0f}s, "
          f"p90 {pct(gaps, .9):.0f}s, p99 {pct(gaps, .99):.0f}s, max {max(gaps)/3600:.1f}h")
        for t in GAPS:
            n = sum(1 for g in gaps if g > t)
            w(f"  gaps > {t//60} min: {n} ({100*n/len(gaps):.1f}%)")
        long_ = [g for g in gaps if g > 3600]
        if long_:
            w(f"  gaps > 1 h: median {statistics.median(long_)/3600:.1f} h, max {max(long_)/3600:.1f} h")
    cold = [c for s in sessions for c in s["cold_rereads"]]
    w(f"\nObserved cold re-reads (call re-created >80% of a >20k context): {len(cold)}")
    if cold:
        w(f"  tokens re-read in total: {sum(c['ctx'] for c in cold):,}; median {statistics.median(c['ctx'] for c in cold):,.0f}; "
          f"median gap before them {statistics.median(c['gap'] for c in cold)/60:.0f} min")
    ctxs = [x for s in sessions for x in s["ctx_series"]]
    if ctxs:
        w(f"\nContext size per call: median {statistics.median(ctxs):,.0f}, p90 {pct(ctxs, .9):,}, max {max(ctxs):,}")
    comps = [c for s in sessions for c in s["compactions"]]
    w(f"\nCompaction events: {len(comps)}")
    for c in comps:
        if c["pre"]:
            w(f"  {c['trigger']}: {c['pre']:,} -> {c['post']:,} tokens ({100*(1-c['post']/c['pre']):.0f}% removed)")
    tc, td = sum(s["tool_chars"] for s in sessions), sum(s["tool_dup_chars"] for s in sessions)
    uc, ac = sum(s["user_chars"] for s in sessions), sum(s["assistant_chars"] for s in sessions)
    w(f"\nContent mix (chars): tool results {tc/1e6:.2f} M, assistant {ac/1e6:.2f} M, user text {uc/1e6:.2f} M")
    w(f"Exact-duplicate tool results (>200 chars, 2nd+ occurrence): {td/1e6:.2f} M chars = {100*td/max(1, tc):.1f}% of tool output")
    w("\nWhole-file compression ratios (raw / compressed):")
    for k in sessions[0]["compressed_bytes"]:
        w(f"  {k:8} {tot/sum(s['compressed_bytes'][k] for s in sessions):5.1f}x")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()
    sessions, seen = [], set()
    for p in sorted(a.root.glob("*/*.jsonl")):
        s = analyse_session(p)
        key = tuple(s["msg_ids"])
        if key and key in seen:  # forked/copied session with identical API calls: count once
            print(f"skipping duplicate session {p.name}")
            continue
        seen.add(key)
        sessions.append(s)
    print(report(sessions))
    if a.json:
        a.json.write_text(json.dumps(sessions), encoding="utf8")


if __name__ == "__main__":
    main()
