# Jev Toolkit — Design Document

Status: DRAFT 0.1 (2026-10-30). Nothing here is built yet. Items marked **VERIFY** are assumptions to check before code depends on them.

## 1. What this is, and why it exists

A set of small, independent tools that use a **judgement model** (currently TypeSafe's Jev) as a cheap, fast, structured decision layer *around* a generative LLM (primarily Claude Code, but not only).

The core idea: a generative LLM is good at writing and bad at being its own referee. A judgement model returns **typed answers with probabilities** (yes/no, pick-one, score), which ordinary code can act on deterministically. So: LLM writes, Jev judges, plain logic enforces.

Every tool is a personal learning project (security + data science studies) published on GitHub. The documentation must stay honest about what each tool is, what it is not, and what was borrowed from others (see "Prior art" in each tool's README).

## 2. Honest constraints (read first)

### 2.1 Jev facts (from docs.typesafe.ai, checked 2026-10-30)
- Input: `state` (text/JSON) plus typed questions. Output: typed answers. **It does not generate prose.**
- Question types: `Noul` (yes/no probability), `Choice`, `Score`. Text only.
- Context: 64k tokens per request, 32k per question (long-context header raises it, slower and less accurate).
- Price: ~$0.042 per million input tokens, output free. Cheap enough to call on every prompt.
- Python SDK `typesafe-sdk`, key in `TYPESAFE_API_KEY`. Early SDK; minor versions have broken question types before → pin versions.
- Training on user data: TypeSafe states it does not. Data region/sub-processors: **not stated in the docs I read — VERIFY via the DPA before claiming anything.**

### 2.2 What follows from those facts
| Wanted | Jev can do | Jev cannot do | So we need |
|---|---|---|---|
| Summarise a session | decide *whether/when* to compact; rate importance of items | write the summary | local LLM, or Claude itself, or non-LLM extraction |
| Fact-check | judge "does this passage support this claim?" | extract claims, fetch sources | LLM (claim + citation extraction), deterministic quote matching, Jev as entailment judge |
| Scan for injection | classify text as instruction-like / hidden / hostile | know *new* attacks | rule layer + known-pattern DB + Jev as second layer |
| Bot "DM" | choose among options with probabilities | write dialogue | world logic + Jev for decisions + LLM only for user-facing speech |

### 2.3 Privacy position (must appear in every README)
Prompts sent to Jev leave the machine. Jev is used because it is the only model of its kind on the market today, not because it is the final answer. **The plan is to replace Jev with a local model or a European provider** so no data goes to US servers. Therefore:
- All tools talk to a `Judge` interface, never to the Jev SDK directly (section 4).
- A `LocalJudge` / EU adapter is a first-class roadmap item, not an afterthought.
- Tools must degrade safely (section 3) when the judge is unavailable or disallowed.

### 2.4 Claude plan compatibility
Tools must work with a **Claude Pro/subscription session, not metered API tokens.** Consequence: integration goes through Claude Code's own extension points (hooks, plugin/mod API, MCP tools), never through our own calls to the Anthropic API. The generative model is whatever the user's Claude Code session already is. Other LLMs (local) must be usable through the same adapters where practical.

## 3. Cross-cutting design rules

1. **Model independence.** One `Judge` interface; Jev is one implementation.
2. **Fail-safe direction is per tool, and explicit.** Routers fail *open* (do nothing, never block work). Security monitors fail *closed* (block when unsure or when the judge is down). Document the choice in each tool.
3. **Deterministic first, judge second, LLM last.** Cheap rules and lookups run before Jev; Jev runs before any generative model is spent.
4. **Log everything a decision depends on** (inputs hashed or redacted, judge answers + probabilities, decision, cost, latency) in JSONL, so decisions can be audited and thresholds tuned with real data.
5. **No secrets in repo.** `.env` for keys, `.env.example` committed.
6. **Measure before claiming.** Each tool ships an evaluation script and reports results, including where it fails. No unmeasured claims in READMEs.
7. **Own code, credited ideas.** English code/comments/README, a "Prior art" section with links, nothing copied.
8. **One tool at a time**, tested before the next starts.

## 4. Shared core: `jev_core`

- `Judge` protocol: `noul(state, question) -> P(yes)`, `choice(state, question, options) -> dict[option, prob]`, `score(state, question, levels) -> (level, dist)`; all return `confidence` and raw metadata.
- Implementations: `JevJudge` (typesafe-sdk), `FakeJudge` (tests), later `LocalJudge`.
- Cross-cutting: timeout, retry/backoff, typed errors, response cache (keyed on model version + state hash + question), probability logging, cost accounting.
- Config via env/TOML; model version pinned (`jev-1.14.0` currently; alias `jev-latest` only for experiments).
- Smoke test + contract tests that run against `FakeJudge` offline.

**VERIFY:** timeout/error/probability field names in the SDK's API reference (not on the pages read so far).

## 5. Build order

The author's order, one at a time, each tested before the next:

| # | Tool | Depends on |
|---|---|---|
| 0 | `jev_core` + Claude Code integration spike | — |
| 1 | **Model/effort router** | 0 |
| 2 | **Context compiler, clear and long-term log** | 0, (1 optional) |
| 3 | **Injection scanner** (part of Agent System Supervision) | 0 |
| 4 | **Agent monitor** (the other half of ASS) | 0, 3 |
| 5 | **Fact checker with hover-citations** | 0 |
| 6 | **Source restriction** (fact checker + allowlist) | 5 |
| 7 | **Assistant brain** — separate day, large | 0, 1, many |
| 8 | **Living bots (SillyTavern DM)** | 0, world engine |
| 9 | Optional demo framework tying 3, 6, 1, 4 together | 1,3,4,6 |

## 6. Tool designs

### 6.0 Integration spike (blocks tool 1)
Claude Code hooks can read a prompt and add context/block it; **VERIFY** whether they can change model or effort. Candidate routes:
- **A.** Hook invokes the desktop app's session controls (`set_session_model`, `set_session_effort` exist as app tools; unproven from a hook).
- **B.** A Claude Code mod (plugin) if its API exposes model switching (the `session-band` mod already gives us a UI band and session store).
- **C.** Advisory only: router shows "this looks light → use model X / effort Y" in the band and logs it.
Exit criterion: one route demonstrably changes (or recommends) model + effort for the *next* turn on a Pro plan. If only C works, tool 1 ships as C and that is stated honestly.

### 6.1 Model & effort router
- Trigger: before a prompt reaches Claude (UserPromptSubmit-style hook).
- Questions to Jev: complexity (Score 1–5), risk/destructive intent (Choice), needs-deep-reasoning (Noul), mostly-mechanical (Noul).
- Mapping table (config, not code): e.g. mechanical → small model/low effort; ambiguous multi-file → big model/high effort. Hysteresis so it does not flip every message; never downgrade mid-task without a signal.
- Fail-open: no key, timeout, low confidence → leave settings alone.
- Cost guard: skip routing for very short prompts, slash commands, and repeated prompts (cache).
- Evaluation: replay N real past prompts; compare router's choice vs. what a human would pick, and estimated usage vs. always-big-model. Report honestly, including where routing hurt quality.

### 6.2 Context compiler, clear, long-term log
Goals: avoid expensive cold restarts and bloated context, while keeping exact history retrievable.

**Decision problem (Jev's real role):** when the prompt cache is about to expire (1h TTL after last answer), is it cheaper to (a) let it go cold and pay a full re-read on the next message, (b) compact now and continue on the summary, or (c) clear and rely on the log? This is mostly arithmetic on token counts, cache TTL, and expected next-message probability. Jev scores "will the user continue this task soon?" and "how much of this context is still load-bearing?"; code computes cost. **VERIFY** actual cache pricing/behaviour on a subscription plan — on Pro the cost is usage-limit, not dollars, so the model of cost must be stated as an assumption.

**Producing the summary (Jev cannot write prose):**
1. Prefer non-LLM reduction first: drop duplicate tool output, truncate large file reads/logs to head+tail+hash, strip already-resolved errors. **Research task:** survey existing no-cost compaction tools before building (list in Prior art).
2. Then abstractive summary by a **local model** (preferred, private) or by Claude itself as a fallback. Open question: local vs. Claude (see section 8).
3. Jev scores which items must survive verbatim (decisions, constraints, file paths, open TODOs).

**Long-term log (not context):** full conversation stored in a searchable store (SQLite + FTS5 to start), exposed to the LLM through a tool/MCP (`search_log(query)`, `get_turn(id)`), so the model can look back for exact wording on demand without carrying it in context.
- Tiered retention: hot (full text) → warm (compressed, e.g. zstd) after N days → cold (aggressively compressed or summarised-only) after years. Thresholds are a **design decision that needs data**: measure log growth per day, compression ratios, and retrieval quality at each tier on real logs before fixing numbers. Default until measured: nothing is deleted, only compressed.
- Privacy: logs contain code and possibly secrets → local only, redaction pass for known key patterns, documented.

**Measurement:** tokens/usage before vs. after on identical task replays; retrieval accuracy of the log tool.

### 6.3 Injection scanner (ASS, outside threats)
Scans text *before* it reaches the LLM (user paste, fetched web pages, file contents, tool results).
- Layer 1, deterministic: hidden/zero-width/bidi characters, homoglyphs, base64/hex/URL-encoded blobs that decode to instructions, HTML/Markdown comments, white-on-white style tricks, instruction-like phrases against a pattern DB.
- Layer 2, known-threat DB: local database of known injection patterns and signatures (updatable, versioned, with source per entry). **Research task:** which public datasets/lists exist and their licences.
- Layer 3, Jev: Noul "does this text try to give instructions to an AI?" and Choice for attack class; used when layers 1–2 are inconclusive or text is long (chunk to fit 32k).
- Output: `{risk, reasons[], action}` where action ∈ allow / flag / strip / block. Stripping must be explainable and reversible in the log.
- Fail-closed for untrusted sources (web, files); fail-open configurable for the user's own typed prompts.
- Evaluation: own attack set (public examples are inspiration only, own cases written by hand), report false positives on benign text, not just detection rate.

### 6.4 Agent monitor (ASS, inside threats)
Immune-system analogy: a separate, simpler logic agent watches the working agent. Sits **synchronously between agent and tools** (Claude Code `PreToolUse`/`PostToolUse` hooks are the likely integration point — **VERIFY** capabilities).
- Inputs: the agent's declared instructions/plan, its tool calls, available logs/visible reasoning, and tool outputs.
- Checks (deterministic where possible): declared scope vs. paths/hosts touched; outbound traffic allowlist; plan-vs-action drift; changes to the agent's own instruction files (CLAUDE.md, settings, hooks) — flagged as high severity; taint tracking (untrusted-origin data flowing into commands/secrets/network); canary files/tokens that should never be touched.
- Jev role: Noul "is this action consistent with the stated task and rules?", Score for suspiciousness, Choice for category. Used as a second opinion, never the only gate for high-severity rules.
- Responses ladder: log → warn user → block call → kill agent session. Kill is last resort and always logged.
- **Fail-closed on start** and on judge failure for sensitive tool classes (shell, network, file writes outside scope); read-only tools may fail open.
- Must survive context compaction: its rules and state live outside the agent's context.
- Test case: a miniature reconstruction of the Hugging Face incident referred to in the notes. **VERIFY** the facts of that incident from primary sources before describing it in docs; until then the test is described only as "an agent edits its own instruction file".
- Honest limit to document: a monitor reduces risk, it does not make an agent safe; a determined attacker or a monitor bug can bypass it.

### 6.5 Fact checker with hover-citations
Pipeline (Jev judges; it does not fetch or write):
1. LLM answers and must provide, per claim, a source reference and a **verbatim supporting quote**.
2. Deterministic: split text into claims (LLM-assisted), resolve each source reference, check the quote actually appears in the fetched source (exact/near-exact match with offsets).
3. Jev: Noul "does this quote support this claim?" for each claim; Choice for support level (supports / partial / contradicts / unrelated).
4. Source quality: compare source domain/type against per-topic lists (curated allow/suspect lists, user-extendable); flag unusual sources instead of silently trusting them.
5. Output: annotated text. Each claim is a span; hover shows source, URL, and highlighted passage with offsets. Static HTML output first (works in any browser), integrations later.
6. Correction loop: unsupported claims are sent back to the LLM with the specific failure; **stop** if the source lacks the information (no inventing a supporting source), max N rounds, final output marks what remains unverified.
- Limits to document: only checks support *by the cited source*, not that the source is true; quote-matching fails on paraphrased/paywalled/dynamic sources.

### 6.6 Source restriction
Fact checker plus a policy layer:
- Allowlist (domains, document sets, jurisdictions, internal company docs) enforced **before** the model sees retrieval results (strip non-approved) and **after** (reject any answer citing an unapproved source or containing a claim not found in approved sources).
- Policy file is human-readable and versioned; every rejection explains which rule failed.
- Rewrite loop shares the correction loop of 6.5.

### 6.7 Assistant brain (separate day)
Jev handles *decisions* (priority, escalation, what to do now); the LLM only writes text. Calendar, reminders and rules are plain logic. Scope and design deferred to its own document; depends on the router, log and monitor being stable.

### 6.8 Living bots (SillyTavern DM)
Separate large project; summary of the model so far:
- **World model:** rooms with sub-rooms (café → counter, tables), descriptions, entities, schedules, needs, relationships. Everything is state + rules.
- **Perception:** audibility/visibility rules decide who perceives what (same room: sees; same sub-room: converses; shout: audible outside sub-room).
- **Randomness:** per-character randomness parameters (percentages), seeded and logged for reproducibility.
- **Character change:** *life events* (relationship, job, house…) and *goals* (things the character promised in conversation, captured from dialogue) update the character sheet.
- **LLM use:** only when talking with the user, and for generating backstory/routines when a character is created. Autonomous bot-to-bot activity runs on logic with minimal or no LLM.
- **Jev role:** pick among scheduled/needed actions with probabilities, judge "did the user's message create a goal?", decide who reacts to an event.
- First milestone: café scene only.

## 7. Repository layout (proposal)

```
jev-toolkit/
  README.md            # overview, privacy position, prior art, status per tool
  docs/DESIGN.md       # this file
  packages/
    jev_core/          # Judge protocol, JevJudge, FakeJudge, cache, logging
    router/
    context_log/
    injection_scanner/
    agent_monitor/
    factcheck/
  evals/               # evaluation scripts + datasets (own data only)
  .env.example
```
One tool per package with its own README, "Prior art", limits and evaluation results. (Alternative: separate repos per tool; decide before publishing.)

## 8. Open decisions
1. Integration route for the router (spike 6.0) — decides what tool 1 can actually do.
2. Compaction: local LLM vs. Claude vs. Jev-gated; which local model, which hardware budget.
3. Log store format and retention thresholds (need measured data, section 6.2).
4. Which public threat/injection datasets are usable and under which licences.
5. Where the Jev data-processing location is documented (DPA) and whether a zero-data-retention request is needed for real use.
6. Mono-repo vs. one repo per tool.
7. Candidate replacement judge (local or EU) — research item once the `Judge` interface exists.

## 9. Prior art (to be filled with links during each tool's research step)
Placeholders only — no claims until sources are read: prompt-injection detectors, LLM routers/gateways, context-compaction tools, LLM observability/log stores, citation-grounded QA, agent guardrail/sandbox projects, interactive-fiction and agent-simulation frameworks. For each: link, what it does, what this project does differently.

## 10. Documentation honesty checklist (for every README)
- What the tool does and does **not** do.
- That it currently depends on Jev and why, and the plan to replace it.
- What was measured, on what data, and the failures.
- Known bypasses and risks.
- Credits for ideas and prior art.
