# Context compiler, clear and long-term log (project 2)

Status: **plan, awaiting approval** (2026-10-30). Phase 1 = research + measurement. Nothing here beyond `src/jev_context/measure.py` is built.
Legend: **[measured]** = computed from Henric's own transcripts with `python -m jev_context.measure`; **[verified]** = stated in Anthropic's docs (code.claude.com prompt-caching and costs pages); **[assumed]** = my assumption, to be checked.

## 1. The problem in one paragraph

A Claude Code conversation carries its whole history as context. That history is cached; when the cache is cold (main conversation: 1 h after the last call, subagents 5 min [verified]) the next message re-reads the entire context at cache-write price. Henric's contexts are large, so a cold restart is expensive; but compacting also costs a request, and clearing loses information. Question: **at a given moment, which of "do nothing", "compact now", "clear and rely on a log" is cheapest, without losing what matters?** Separately: keep a complete, searchable log outside the context for exact look-back, and keep that log from filling the disk over years.

## 2. Prior art (research done 2026-10-30; licences read from each repo's page, not audited)

| Tool | What it does | LLM? | Licence | What differs from this project |
|---|---|---|---|---|
| [sqz](https://github.com/ojuschugh1/sqz) | Compresses tool output before it enters context; SHA-256 session dedup (`§ref:HASH§` instead of repeats); Claude Code hooks + MCP | No, deterministic Rust | Apache-2.0 or MIT (≤2.0.0 was Elastic-2.0) | Works *per tool call going in*; no cost model for cold/warm, no long-term log. A candidate to **use alongside**, not to copy. |
| [Headroom](https://github.com/chopratejas/headroom) | Content-routed compressors (JSON, AST-aware code, prose via a small trained model), cache-aware (never rewrites the cached prefix), reversible: originals stored locally, retrievable by tool | Small trained model for prose, not a generative LLM | Apache-2.0 | Closest in spirit (reversible compression + retrieval). Compresses on the way in; does not decide *when* to compact vs let the cache go cold. |
| [LLMLingua](https://arxiv.org/abs/2310.05736v1) (Microsoft) | Token-level prompt compression using a small LM's perplexity | Small LM (not generative output) | Reported as MIT by package indexes; **repo LICENSE not read by me** | Needs a local model + GPU-ish compute, lossy at token level; wrong granularity for code/tool output. Reference only. |
| [claude-mem](https://github.com/thedotmack/claude-mem) | Hooks record sessions, an AI generates summaries, SQLite + FTS5 + Chroma, 3-step retrieval (search → timeline → get) | **Yes** (summaries via an AI provider) | Apache-2.0 | Memory across sessions via LLM summaries. We want verbatim log + non-LLM reduction + cost decision. Its 3-step *retrieval pattern* is worth echoing in our tool API. |
| [claude-session-search](https://pypi.org/project/claude-session-search/), [Memex (vimo-ai)](https://lobehub.com/mcp/vimo-ai-memex), [tim0120 history MCP](https://lobehub.com/mcp/tim0120-claude-code-history-mcp), [context-memory](https://enterprisedna.co/directories/mcp/erebusenigma-context-memory) | Index Claude Code transcripts into SQLite/FTS5, search via web UI or MCP | No | **not checked** | Prove the log-search idea works and is common. None has tiered retention, secret redaction at ingest, or the cold/warm decision. Memex notes the 30-day deletion of local transcripts, which motivates our own store. |
| [context-compress](https://github.com/vidanov/context-compress) | Regex compression of instruction files (CLAUDE.md) | No | not checked | Static files only. |
| Built-in `/compact`, `idleCompaction` | LLM summary by Claude itself | Yes | n/a | The baseline we compare against; costs one large request when cold [verified]. |

Take-aways: (1) non-LLM compaction of *tool output* exists and is mature (sqz, Headroom) — we should not rewrite it; (2) transcript → SQLite/FTS5 search exists many times over — our value is **redaction, tiering with measured thresholds, and the cost-aware decision**; (3) nobody found does the cold-vs-warm-vs-clear decision with a judge. Gaps in this survey: I did not read the source of any of these, and star counts / maintenance state were not checked.

## 3. What the data says [measured]

Source: 12 transcript files under `~/.claude/projects` (11 after removing one byte-identical fork), 23.6 MB, 611 main-chain API calls, 2026-10-01 … 10-30. **Small sample, one user, mostly three long sessions** — thresholds below are starting points, not findings about "users".

| Quantity | Value |
|---|---|
| Context per API call | median 263k tokens, p90 569k, max 726k |
| Gap between consecutive calls | median 12 s, p90 65 s, p99 64 min, max 75.7 h |
| Gaps > 5 min / > 1 h | 16 (2.7 %) / 7 (1.2 %); the >1 h gaps have median 3.4 h |
| Observed cold re-reads (call re-created >80 % of a >20k context) | 6, median 387k tokens each, 2.63 M tokens total |
| Cache tokens created vs read, all sessions | 5.68 M created, 177 M read → creation is 3.1 % of cache traffic, **but 46 % of all creation was these 6 cold re-reads** |
| Compactions | auto 343,780 → 39,251 tokens (−89 %); manual 728,023 → 18,737 (−97 %) |
| Tool results share of transcript text | 1.60 M chars vs 0.13 M assistant + 0.21 M user: **tool output is ~82 % of content** |
| Exact-duplicate tool results (>200 chars) | only 1.2 % of tool output → plain dedup (sqz-style) saves little *here*; truncation/summarising of big reads is where the volume is |
| Transcript size by record type | user 28 %, assistant 27 %, attachment 17 %, **api-request/-blob/-shape 25 %** (copies of request bodies; pure log overhead, droppable) |
| Whole-file compression | zlib-6 4.0×, zstd-3 6.8×, zstd-19 8.3×, lzma 8.5× |
| Log growth | ~23.6 MB over the month, heaviest day ~9 MB raw → ≈ 1.3 MB/day zstd-3 at the heavy end |

Honest limits: "cold" is inferred from usage numbers, not from an explicit flag; the 5-minute subagent gaps are not separated; the sample has only 2 compactions, so no distribution of compaction quality; retrieval quality of any tier is **not measured yet** (needs the Phase 2 store).

Reading: cold restarts are **rare (1.2 % of gaps) but dominate creation cost** because contexts are huge. So the interesting decision is the rare one: "I have a 400k context and the cache is about to expire (or has)".

## 4. Decision model

### 4.1 Cost units — what is verified, what is assumed
On a subscription, cost is **usage against a limit, not dollars**, and Anthropic does not publish the per-token weighting. I use *relative weights borrowed from API list ratios* purely as an assumption: cache read r = 0.1, cache write (1 h TTL) w = 2.0, uncached/other input 1.0, output o = 5.0, all per token, in "input-token equivalents". The break-even formulas below keep r, w, o as parameters; Phase 2 reads them from config and the plan includes calibrating them against the app's own usage numbers (`$.session.usage`, usage band) so they are not guesses forever. [verified]: separate cache per model; model switch or some setting changes re-read everything; main conversation cache 1 h, subagents 5 min; `/compact` is cheap-ish warm, a large request cold.

### 4.2 Options when context is C tokens, summary size S, expected future messages n
Let state be **warm** (cache alive) or **cold**.

- **A. Do nothing / continue.** Warm: each future turn costs r·C. Cold: the next turn costs w·C once, then r·C per turn.
- **B. Compact now.** The compaction call reads C (warm: r·C, cold: w·C) and writes a summary of S output tokens (o·S). The next turn then writes S fresh (w·S), and later turns cost r·S.
- **C. Clear and rely on the log.** Cost ≈ w·S₀ where S₀ is the tiny re-seed (a few k tokens: goal, constraints, pointers). Loses everything not in the seed; recoverable only through log search. Chosen when most of C is *not load-bearing*.

Per-turn saving after compacting is r·(C − S). Extra one-off cost of B over A:
- warm: Δ = o·S + w·S (the r·C read is paid by A too on its next turn, so ≈ cancels) 
- cold: Δ = o·S + w·S as well (A pays w·C once, B pays w·C inside the compact call instead)

so **break-even number of future turns n\* = (o + w)·S / (r·(C − S))**. With C = 263k (median), S = 20k, o=5, w=2, r=0.1: n\* = 140k / 24.3k ≈ **5.8 turns**. With C = 569k (p90): ≈ 2.6 turns. With C = 60k: ≈ 35 turns. So under these (assumed) weights compaction pays off only when context is large *and* the user will keep going; at small C leave it alone. The same formula says compacting a *cold* session buys nothing extra over letting the next message re-warm it, unless n > n\* — the only reason to compact cold is that n is large (or context quality/limit pressure, §4.4).

Note the "wait" option has a time dimension: if the cache is warm and the user is idle, waiting costs nothing until the TTL, then A-cold applies. An early compact while warm vs a later one cold costs the same Δ here (a premise worth verifying: compact-while-cold may also re-create the cache for the prefix, making it slightly better than I model).

### 4.3 What plain arithmetic covers, what needs Jev
| Needed input | Source |
|---|---|
| C, TTL remaining, model, idle time | `$.session.usage`, timestamps — plain code |
| S (summary size) | estimate from measured ratios (−89 % … −97 % ⇒ S ≈ 0.03–0.11·C) — plain code |
| r, w, o weights | config, calibrated (§4.1) |
| n = expected number of further turns in this task | **Jev** (below) — not knowable by arithmetic |
| fraction of C that is load-bearing | **Jev** per chunk, or cheap heuristics first (tool output older than k turns, files since re-read) |
| items that must survive verbatim | **Jev** scores candidates; code extracts them |

Jev's typed questions (one `state` per question: last user prompts + task heading; never raw secrets — run the router's redaction first):
1. `Noul`: "Will the user most likely continue this same task within the next hour?" → P_continue. Expected n comes from a small table keyed by this probability and the Score below (e.g. P>0.8 and task=ongoing ⇒ n≈10).
2. `Score` 1–5: "How much of the earlier conversation is still needed for the next steps?" → load-bearing level.
3. `Choice` ["task finished", "paused mid-task", "topic switch"]: closure state. Finished/switch ⇒ option C (clear) becomes attractive.
4. For each candidate item (decision, constraint, file path, TODO, error that was resolved): `Noul` "must this be kept verbatim in a summary?" Batched, ≤32k tokens per question.

Everything Jev returns is advisory input to the formula; fail-open (judge down ⇒ don't compact, show nothing). Decision output: one of `keep`, `compact_now`, `compact_before_ttl(minutes)`, `clear_with_seed`, with the numbers shown.

### 4.4 Non-cost reasons to compact
Context limits and long-context accuracy (the 726k-token session is near the limit) — compacting early may be right even when n < n\*. Config switch: `max_context_tokens` above which the answer is "compact" regardless of arithmetic.

## 5. Reducing context without an LLM
Order of operations (cheapest first), all deterministic, applied by *us* only to the **log and to compaction input**, never by rewriting cached history in place (that would break the cache; Headroom makes the same point):
1. Drop duplicate tool results by hash (measured gain small: 1.2 %).
2. Truncate big tool results (file reads, logs) to head + tail + hash, original kept in the log (reversible; same idea as Headroom/sqz).
3. Remove resolved errors and superseded file versions.
4. Only then summarise: Claude's own `/compact` (via `$.session.compact`, no API key needed) or a local model. Jev only chooses *what* and *when*.

## 6. Long-term log design

**Store:** SQLite, `var/log/jev-context.sqlite`, local only. Tables: `session(id, project, started, model)`, `turn(id, session_id, seq, ts, role, kind, text, tokens, redacted)`, `blob(id, turn_id, sha256, zstd_bytes)` for large tool output, FTS5 virtual table over `turn.text` (`unicode61`, trigram optional). Ingest from the transcript JSONL (and later from `turn.complete`), **excluding `api-request*` records (25 % of bytes, no information not already present)**.

**Redaction before write:** reuse `jev_router.redact` patterns (API keys, tokens, `.env` lines, private key blocks); store a count of redactions, never the secret; test with seeded fake secrets.

**LLM access:** an MCP server / mod tool with the 3-step pattern: `search_log(query, limit)` → ids + snippets; `get_turn(id, context=2)`; `get_blob(id)`. Output size capped so a search can't re-inflate the context.

**Tiering — proposal, to be validated in Phase 2:**
| Tier | Age | Form | Why |
|---|---|---|---|
| hot | 0–30 days | rows in SQLite, text uncompressed, FTS5 index | exact, fast; the 30-day mark also matches Claude Code's own transcript deletion |
| warm | 30 days – 2 years | per-session zstd-19 blob of the full turns (≈ 8×), FTS index kept for text of user/assistant turns only | measured 6.8–8.3× whole-file; tool bodies searchable only after un-pack |
| cold | > 2 years | user + assistant text kept (zstd/lzma), tool results reduced to head/tail + hash + size, original dropped; optional LLM/local summary per session | tool output is ~82 % of text, so this is where the space is; lossy and irreversible, hence late and opt-in |

Disk arithmetic with the measured numbers: ~1.3 MB/day at the heavy end ⇒ ≈ 0.5 GB per year in warm form, more like 0.05–0.15 GB/year at typical days. **The data says disk is not urgent**; the multi-year cold step is a hygiene decision, so default stays "nothing is deleted, only compressed" (as in DESIGN §6.2) and the cold tier ships disabled. FTS index size, retrieval quality per tier and the lossy-tier information loss are **not measured** — Phase 2 measures them (index overhead as % of text; recall of 30 known-answer queries before/after each tier).

## 7. Implementation plan (for approval)

Smallest useful slice, in order, each with offline tests and one live run:
1. **`jev_context.cost`** — the §4 arithmetic as pure functions (`break_even_turns`, `decide`), config for r/w/o, tests with the worked numbers above.
2. **`jev_context.ingest` + `store`** — transcript JSONL → SQLite+FTS5, drop `api-request*`, redaction, idempotent by record uuid; tests with synthetic transcripts and seeded secrets; run on Henric's 11 real sessions and report size, ingest time, FTS overhead.
3. **`jev_context.search`** — CLI `python -m jev_context search "…"` plus `get_turn`; evaluation: 30 hand-made known-answer queries against real sessions → recall@5.
4. **`jev_context.advisor`** — builds the 4 Jev questions through `jev_core.Judge` (with `FakeJudge` in tests), feeds `cost.decide`, logs every decision via the existing decision log; live run against Jev (`python -m jev_core smoke` first). **Advisory only** — shows "compact now / let it cool / clear" and the numbers; no auto-compact in this slice.
5. **Tiering job** (`warm` only) and measured compression table; `cold` stays designed-not-enabled.
6. **Later, separate approval:** mod integration (status line + `compact` button using `$.session.compact`), MCP tool for the LLM, auto-compact before TTL.

Out of scope for the slice: local-LLM summariser, semantic/vector search, cold-tier deletion.

Open decisions for Henric: (a) accept the assumed r/w/o weights for now, calibrate later? (b) hot window 30 days and warm/cold boundary at 2 years OK as defaults? (c) MCP tool (LLM can search) in this slice or after the advisor? (d) use sqz/Headroom alongside later, or keep everything first-party? 
