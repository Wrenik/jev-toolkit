# Jev Toolkit — Roadmap

Last updated: 2026-10-30. Design background: [DESIGN.md](DESIGN.md) (sections 5 and 6 = build order and per-tool designs, section 8 = open decisions). This file is the to-do list; DESIGN.md stays the source for *how*.

Rules for every project: one at a time, tested before the next starts (DESIGN 3.8). Fail-safe direction is stated per tool (DESIGN 3.2). Everything talks to the `Judge` interface, never to the Jev SDK directly (DESIGN 4). Must work on a Claude Pro/subscription session, so integration goes through hooks, mods or MCP tools, not our own Anthropic API calls (DESIGN 2.4).

## Overview (build order)

- [x] 1. Model and effort router
- [ ] 2. Context compiler, compressed context and searchable long-term log **← NEXT**
- [ ] 3. ASS, outside threats (injection scanner)
- [ ] 4. ASS, inside threats (agent monitor)
- [ ] 5. Fact checker with hover citations
- [ ] 6. Source restriction
- [ ] 7. Assistant brain (large, its own day, last but one)
- [ ] 8. Living bots / SillyTavern DM (large, last)
- [ ] Cross-cutting and housekeeping (below; can be done between projects)

### Consistency with DESIGN.md
The order matches DESIGN section 5 (tools 1–8). Differences, deliberately not edited in DESIGN.md:
- DESIGN 5 lists an optional tool 9 (demo framework tying 3, 6, 1, 4 together). Not one of the author's projects; kept here only as an optional idea after project 8.
- DESIGN 6.2 leans towards "non-LLM reduction first, then a local model or Claude for the summary". The author's wording is "check for existing no-cost tools first". Treated as the same step 1 of research; the roadmap makes the survey the first deliverable.
- DESIGN 7 proposes `packages/router/` etc. The real code lives in `jev_core`, `jev_router` and `claude-mod/jev-router`, and project 2 starts under a new package. Layout in DESIGN 7 is therefore out of date.
- DESIGN 3 header says "Nothing here is built yet"; project 1 is now done (commit c5724b3).
- DESIGN 6.4 mentions a Hugging Face incident "referred to in the notes"; it is unverified (see Cross-cutting).

## Common opening for every start prompt
Each prompt below begins with this block (repeated in each, so it can be pasted alone):
> Write all code, comments and README in English (the author speaks Swedish in chat, repo files stay English). Read `docs/DESIGN.md` and `docs/ROADMAP.md` first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building. Commit only your own paths; other sessions may work in the folder.

---

## 1. Model and effort router — DONE
- [x] Done (last commit c5724b3, `jev_core`, `jev_router`, `claude-mod/jev-router`).

**Goal.** Jev scores each prompt (complexity, risk, needs deep reasoning, mechanical) and a Claude Code mod sets model and effort for the next turn. Fails open.
**Depends on.** `jev_core`.
**Open.** Threshold tuning needs real data (see housekeeping). Integration limits that were found are documented in the router README.
**Done when.** Met: works on a Pro session, logs to `var/router-log.jsonl`, bug report of 2026-10-30 fixed.

---

## 2. Context compiler and long-term log — NEXT
- [ ] Prior-art survey written
- [ ] Cost decision logic built and tested offline
- [ ] Reduction (compression) pipeline built
- [ ] Searchable log store + tool for the LLM
- [ ] Retention measurements done and thresholds chosen
- [ ] README, evals, results

**Goal.** A Jev link that logs every session and keeps context small. Two jobs. (a) Context: decide *whether compacting now is worth it*, or whether it is cheaper to let a cold session re-warm uncompressed (the prompt cache lives about 1 hour on the main conversation of a subscription; check). Jev judges "will work continue soon?" and "how much is still load-bearing?", code does the arithmetic. (b) Log: the full conversation is stored searchably, is *not* context, and the LLM can query it on demand (`search_log`, `get_turn`). Long-term logs get a much later compression date (years) so they do not fill the disk.
**Depends on.** `jev_core`. Router optional (shares the Claude Code hook/mod integration).
**Open research / decisions.**
- First: do tools exist that compress context without an LLM or without cost? Survey before building; reuse ideas (credited), not code.
- If none: non-LLM reduction (dedupe tool output, head+tail+hash for big reads) first; abstractive summary by local model or Claude is an open choice (DESIGN 8.2). Jev cannot write prose.
- Cost model on a subscription is usage-limit, not dollars: state it as an assumption; verify cache TTL and behaviour (**unknown**).
- Retention tiers (hot, warm, cold) need data: log growth per day, compression ratio, retrieval quality per tier. Until measured: nothing deleted, only compressed (DESIGN 6.2).
- Log store: SQLite + FTS5 to start. Redact key patterns. Local only.
**Definition of done.** Survey with links; decision function with tests (FakeJudge) and a replay showing usage before/after on identical tasks; searchable log reachable from Claude Code; measured compression and retrieval accuracy per tier with the chosen thresholds and the data behind them; README states fail-safe direction and limits.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 4, 6.2, 8) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: context compiler and long-term log (roadmap project 2). Step 1 is research only: find existing tools that compress or compact LLM/Claude Code context without an LLM or without cost, and summarise what they do and where they fall short. Then design: Jev (via the Judge interface) decides whether compacting now is worth it versus letting a cold session re-warm uncompressed (prompt cache about 1 hour on a subscription's main conversation - verify). Also design a searchable full log (not context) the LLM can query on demand, with a retention plan where long-term logs are compressed much later (years); the thresholds must come from measured data (quality vs log size), so include a measurement plan. Work under a new package, not in jev_router. Put the plan in front of the author before coding.
```

---

## 3. ASS, outside threats (injection scanner)
- [ ] Prior-art and dataset/licence survey
- [ ] Deterministic layer
- [ ] Known-threat database layer
- [ ] Jev layer
- [ ] Own attack set + false-positive evaluation

**Goal.** Detect outside attempts to compromise an LLM (hidden prompts, injected instructions in pasted text, web pages, files, tool results). Checks against databases of known threats; stops and flags.
**Depends on.** `jev_core`. Hook points shared with project 2 and 4.
**Open.** Which public injection datasets/lists exist and their licences (DESIGN 8.4). How to keep the database updated and versioned. Fail-closed for untrusted sources, configurable fail-open for the user's own typed prompts. Output is `{risk, reasons[], action}` with action allow/flag/strip/block, stripping explainable and logged.
**Definition of done.** Three layers (deterministic, known-threat DB with source per entry, Jev for inconclusive or long text) working in a Claude Code hook; hand-written attack set; reported detection rate *and* false positives on benign text; known bypasses written down.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 4, 6.3) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: Agent System Supervision part 1, outside threats (roadmap project 3). Build an injection scanner that checks text before it reaches the LLM: layer 1 deterministic (hidden/zero-width/bidi characters, encoded blobs, comments, instruction-like phrases), layer 2 a versioned local database of known threats with source per entry, layer 3 Jev through the Judge interface for inconclusive cases. It must stop and flag, fail closed for untrusted sources. Research public datasets and their licences first; write the evaluation attack set by hand and report false positives. Put the plan in front of the author before coding.
```

---

## 4. ASS, inside threats (agent monitor)
- [ ] Hugging Face incident researched from primary sources (or dropped)
- [ ] Prior-art survey (firewall/IDS/IPS ideas, agent guardrails)
- [ ] Deterministic checks
- [ ] Jev second-opinion layer
- [ ] Response ladder (log, warn, block, kill)
- [ ] Evaluation with a miniature incident test

**Goal.** An outside logic agent (like Jev) watches the working LLM, modelled on network security (firewall, IDS/IPS) and the body's immune system. It reads logs and visible reasoning before and during work and flags odd behaviour: actions versus its instructions, abnormal tool use, changed instructions, tool use that looks exploitable. Alerts, logs and, in an emergency, kills the agent.
**Depends on.** `jev_core`; project 3 (shared taint/threat ideas).
**Open.** What Claude Code `PreToolUse`/`PostToolUse` hooks can actually do (**VERIFY**); can a session be killed from a hook? The Hugging Face incident: **its facts are not verified.** Research primary sources before describing it anywhere; until then the test case is only "an agent edits its own instruction file". The monitor's rules must live outside the agent's context and survive compaction. Fail-closed for sensitive tool classes (shell, network, writes outside scope).
**Definition of done.** Monitor runs synchronously in hooks; deterministic checks (scope, outbound allowlist, instruction-file changes, canaries); Jev only as second opinion; response ladder with logged kill; evaluation with a hand-built scenario set including false alarms; README states plainly that a monitor reduces risk and does not make an agent safe.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 4, 6.4) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: Agent System Supervision part 2, inside threats (roadmap project 4). An outside logic agent (like Jev, via the Judge interface) supervises a Claude Code agent, borrowing ideas from firewalls and IDS/IPS: compare actions with instructions, flag abnormal tool use, flag changed instructions, flag exploitable tool use; alert, log, and kill only as a last resort. First verify what Claude Code hooks can do. the author mentioned a Hugging Face incident: do NOT describe it as fact until you have read primary sources; report what you can and cannot confirm. Put the plan in front of the author before coding.
```

---

## 5. Fact checker with hover citations
- [ ] Prior-art survey (citation-grounded QA, entailment checkers)
- [ ] Claim + source + verbatim quote format
- [ ] Deterministic quote matcher with offsets
- [ ] Jev support judgement
- [ ] Source-quality lists per topic
- [ ] Annotated HTML output (hover shows source and passage)
- [ ] Correction loop and evaluation

**Goal.** Jev has the LLM list its sources; sources are compared with per-topic source lists, abnormal ones reported; the text is checked against what each source says; hallucinations go back for correction. The result is markable text: hovering a claim shows which source and which part of it, so everything is traceable.
**Depends on.** `jev_core`. Independent of 2 to 4.
**Open.** Claim extraction needs an LLM (Jev cannot write); who curates the topic source lists; handling paraphrase, paywalls, dynamic pages. The correction loop must stop when the source lacks the information (no invented support), with a max round count. Limit to document: it checks support *by the cited source*, not that the source is true.
**Definition of done.** Static HTML output with hover highlights and offsets; measured agreement with hand-labelled claims (incl. failures); stop conditions tested.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 4, 6.5) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: fact checker with hover citations (roadmap project 5). The LLM lists a source and a verbatim quote per claim; code checks the quote exists in the source; Jev (Judge interface) judges whether it supports the claim; source domains are compared with per-topic lists and unusual ones reported; unsupported claims are sent back for correction with a round limit. Output is HTML where hovering a claim shows source and passage. Put the plan in front of the author before coding.
```

---

## 6. Source restriction
- [ ] Policy file format (human-readable, versioned)
- [ ] Pre-filter of retrieval and post-check of answers
- [ ] Rejection explanations and rewrite loop
- [ ] Evaluation

**Goal.** Like project 5, but the answer also fails (the LLM must rewrite) when a source is not on an allowed list, so users control which sources are used, e.g. their employer's documents or one country's legislation.
**Depends on.** Project 5 (shares claim checking and correction loop).
**Open.** Policy granularity (domain, document set, jurisdiction); how to detect that a claim came from model memory instead of an approved source; behaviour when approved sources are insufficient (should say so, not invent).
**Definition of done.** Policy enforced before and after generation; each rejection names the rule; tests with approved and forbidden sources; honest note on claims that cannot be traced.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 6.5, 6.6) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: source restriction (roadmap project 6), built on the fact checker from project 5. Add a human-readable, versioned allowlist policy (e.g. employer documents, one country's legislation). Enforce it before the model sees retrieval results and after it answers; an answer citing an unapproved source fails and the LLM must rewrite. Every rejection must say which rule failed. Put the plan in front of the author before coding.
```

---

## 7. Assistant brain (large; its own day; last but one)
- [ ] Separate design document written first
- [ ] Rule engine (calendar, reminders)
- [ ] Jev decision layer
- [ ] LLM text-only layer
- [ ] Evaluation

**Goal.** Calendar, reminders and rules are logic; Jev decides priority, escalation and what to do now; the LLM only writes text.
**Depends on.** `jev_core`, router, log and monitor stable (DESIGN 6.7). Needs many parts.
**Open.** Everything beyond the above: scope, data sources, privacy of personal data (calendar and mail go through the judge, so a local/EU judge matters most here), what "escalation" means. Start with a design doc, not code.
**Definition of done.** Design doc approved; then a thin vertical slice (one rule type, one decision, one message) tested before widening.

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 6.7) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here. Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: assistant brain (roadmap project 7), a large project done on a day of its own. Calendar, reminders and rules are plain logic; Jev (Judge interface) decides priority, escalation and what to do now; the LLM only writes text. Step 1 is a design document only: scope, parts, data and privacy handling, and a thin first vertical slice. No code before the author approves the design.
```

---

## 8. Living bots (SillyTavern DM; large; last)
- [ ] Design document (world model first)
- [ ] World engine: rooms, sub-rooms, perception
- [ ] Schedules, routines, per-character randomness (seeded and logged)
- [ ] Life events and goals
- [ ] Jev decision layer
- [ ] LLM for user conversation and backstory only
- [ ] Milestone 1: café scene

**Goal.** Logic plus Jev create a living world where bots interact with the user and each other and move between "rooms" that limit who they can interact with (café with tables and a counter as sub-rooms; people elsewhere can still hear a shout or see someone enter). Bots run mostly on rules, routines and schedules with minimal LLM; the LLM is used almost only when talking to the user, plus a believable backstory. Per-character randomness (percentages). Events that change characters (couples forming and breaking up, jobs, buying a house) and goals (things a bot said in conversation it would do).
**Depends on.** `jev_core`, a world engine built as part of this project. How it plugs into SillyTavern (extension, API) is **not yet researched**.
**Open.** SillyTavern integration route; how goals are extracted from dialogue (Jev judges "did this create a goal?", an LLM may be needed to phrase it); simulation tick design; cost of many bots.
**Definition of done.** Milestone 1: café scene runs with several bots, a user can enter and talk, perception rules (sub-room, shout, entering) work, and a long bot-only run costs near-zero LLM calls (measured).

**Start prompt**
```text
Write all code, comments and README in English. Read docs/DESIGN.md (sections 2, 3, 6.8) and docs/ROADMAP.md first. Add a "Prior art" section to the README with links to similar tools and what differs here (interactive-fiction and agent-simulation frameworks, SillyTavern extensions). Write your own code, copy nothing. Start with a plan and ask the author to approve it before building.

Project: living bots for SillyTavern (roadmap project 8). A rule-based world with rooms and sub-rooms, perception rules (see, converse, hear a shout), schedules and routines, seeded per-character randomness, life events and goals; Jev (Judge interface) picks among actions and judges dialogue; the LLM is used almost only when talking with the user and for backstory. First milestone: the café scene (counter and tables as sub-rooms). Step 1 is a design document and a SillyTavern integration check; get the author's approval before any code.
```

---

## Cross-cutting and housekeeping
- [ ] **Publish on GitHub** (another session is doing this now; check the result and tick it).
- [ ] **Local/EU judge adapter.** Research candidates, then write `LocalJudge`/EU adapter behind the `Judge` interface. Research item; privacy plan in DESIGN 2.3.
- [ ] **Verify Jev data location**: read the TypeSafe DPA; decide whether zero-data-retention is needed (DESIGN 8.5).
- [ ] **Request-count check**: compare the TypeSafe console's request count with the request ids in `var/judge-log.jsonl` (core logs ids since 77268c6).
- [ ] **Tune router thresholds** after about a week of data in `var/router-log.jsonl`; report what changed and why.
- [ ] **README and licence final touches** (overview README, per-tool status, licence file, privacy position in each README).
- [ ] **Hugging Face incident fact-check** before it is written about anywhere (primary sources; if unconfirmed, say so or drop it).
- [ ] **Mono-repo vs one repo per tool** decision (DESIGN 8.6) and update the DESIGN layout section to the real package names.
- [ ] Optional after project 8: demo framework tying projects 3, 6, 1 and 4 together (DESIGN 5, tool 9).

## How to use this file
Pick the first unchecked project in the overview, paste its start prompt into a fresh session, approve the plan, and tick the boxes as work lands. When a project is done, update its status here and add the commit hash. If reality differs from DESIGN.md, note it here first.
