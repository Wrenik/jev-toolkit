# jev-router bug report — 2026-10-30

Scope: `claude-mod/jev-router` (the Claude Code mod), `src/jev_router` (policy + local server) and the parts of `src/jev_core` they use. Commit reviewed: `77268c6`.

Method: full read of the code, then each suspected problem was checked one of three ways, stated per finding:
- **Reproduced**: shown with a live test against the running server or the policy code.
- **Doc-backed**: the Claude Code mod API types say the behaviour happens; not triggered live.
- **Code-evident**: follows directly from the code; not triggered live.

Nothing in this report has been fixed yet.

## Summary

| ID | Severity | Finding | Evidence |
|---|---|---|---|
| H1 | High | On Fable (or any model the router doesn't know) it switches away and skips the warm-cache guard | Reproduced |
| H2 | High | Every prompt goes to TypeSafe automatically, with no secret redaction | Code-evident |
| H3 | High | Can step down to Haiku with a very large (cold) context | Reproduced (policy) |
| M1 | Medium | Effort is never changed when the model is already right | Code-evident |
| M2 | Medium | A message typed while Claude works can be dropped and delayed until the turn ends | Doc-backed |
| M3 | Medium | `@file` mentions are lost when a prompt is resent after a switch | Doc-backed |
| M4 | Medium | The server keeps running old code after an update; no restart command | Observed 3 times today |
| M5 | Medium | Hard-coded Windows paths: the mod only works on this machine | Code-evident |
| M6 | Medium | The mod itself has no automated tests | Code-evident |
| M7 | Medium | The leftover `jev-spike` mod logs the start of every prompt in plain text | Code-evident |
| L1 | Low | Server doesn't check the `Host` header (DNS-rebinding) | Reproduced |
| L2 | Low | A JSON body that isn't an object crashes the request thread | Reproduced |
| L3 | Low | A negative `Content-Length` hangs a request thread forever | Reproduced |
| L4 | Low | Subagent turns reset the "idle since" clock | Doc-backed |
| L5 | Low | In advise mode the router remembers a tier you never ran | Code-evident |
| L6 | Low | `!heavy` with nothing after it is not recognised | Code-evident |
| L7 | Low | Mode is global for all sessions | Code-evident |
| L8 | Low | Logs and cache grow without limit | Code-evident |
| L9 | Low | Context other plugins attached to a prompt is lost on resend | Code-evident |
| L10 | Low | `opusplan` is treated as plain Opus and can be replaced | Reproduced (policy) |

## High

### H1 — Unknown current model (e.g. Fable) disables the guard and causes downgrades
`tier_of_model()` only knows `haiku`, `sonnet`, `opus`. For `claude-fable-5-1` (and the alias `best`) it returns `None`, so the step-down guard has no current tier to protect.

Reproduced with the policy and a 150k-token warm context:
- On Fable, heavy coding prompt → `opus` (a downgrade from Fable, and the 150k cache is thrown away).
- On Fable, trivial prompt → `haiku`, same cache loss.

In auto mode the mod would then switch the session. Anthropic recommends Fable for the hardest work, so this hits exactly the sessions that matter most.

Fix: treat a model the router doesn't recognise as "hands off" (advise only, never auto-switch), or add Fable as a tier above heavy.

### H2 — All prompts are sent to TypeSafe automatically, unredacted
The README tells people not to send secrets to the judge, but with the mod on, every prompt (up to 24,000 characters, pastes included) goes to TypeSafe without the user choosing to. A pasted `.env`, token or private key leaves the machine. TypeSafe doesn't document where data is processed.

Fix: a redaction pass before sending (API-key, token, private-key and password patterns), skip routing when the prompt looks like it contains a secret, and say plainly in `/jev` and the README that routing sends prompt text off the machine.

### H3 — Step-down to Haiku with a huge context
The guard allows a step down whenever the cache is cold, regardless of context size. Reproduced: 150k context, cold cache, trivial prompt → `haiku`. Anthropic's model-config docs say Haiku 5.5 costs more per token above 100k-token prompts. The context-window limits of each model were not checked here; if Haiku's window is smaller than the current context, the switch would also force compaction or fail. **Not verified.**

Fix: never choose `light` when the context exceeds ~100k tokens.

## Medium

### M1 — Effort is ignored when the model already matches
In `register.tsx` the mod returns early when the model is right (`if (isRightModel) return next(...)`), so `/effort` only ever runs as part of a model switch. "Sonnet doing real coding gets `high`" therefore never takes effect while you are already on Sonnet, and Opus/medium vs Opus/high is never adjusted. Anthropic's prompt-caching docs say that on the 5.5 models with a subscription, changing effort keeps the cache, so an effort-only change is cheap.

Fix: track the last effort the mod set, and run `/effort` alone when only the effort differs. (The mod can't read the current effort before a turn, so it can only know what it set itself.)

### M2 — Messages typed mid-turn get delayed
The engine queues prompts typed while Claude is working. If the router decides on another model, the mod drops the prompt and the resend waits until the session is idle. A correction you send mid-task ("stop, use X instead") then doesn't arrive until Claude has finished the wrong thing.

Fix: track `turn.start`/`turn.complete`; while a turn runs, never drop — advise only.

### M3 — `@file` mentions are lost on resend
The mod API docs: `@file` mentions and pasted images "are not expanded for a plugin's prompt". The auto-switch resends with `$.prompt.submit`, so `@src/x.py` arrives as plain text and Claude doesn't get the file attached.

Fix: if the prompt contains `@` mentions, advise only (as is already done for images).

### M4 — Stale server
The Python server is a long-lived process; editing `src/jev_router` doesn't reach it. Today it had to be killed by hand three times. The mod's health check only asks "is something answering", not "is it the current code".

Fix: `/jev restart`, and a version or code hash in `/health` that the mod compares with the code on disk.

### M5 — Not portable
`TOOLKIT` and `PYTHON` are hard-coded Windows paths (`C:/Users/Henric/...`, `.venv/Scripts/python.exe`). Anyone installing from GitHub gets a mod that never starts the server.

Fix: `userConfig` for the toolkit path, and pick `Scripts/python.exe` or `bin/python` by platform.

### M6 — No tests for the mod
The Python side has 38 tests. The TypeScript mod, where most of the bugs in this report live, has none. `claude plugin test` exists for this.

Fix: tests for origin filtering, override parsing, advise vs auto, headless, attachments, mentions, and a mid-turn prompt.

### M7 — Leftover spike mod logs prompt text
`jev-spike` is still loaded in this session and writes the first 30 characters of every prompt to `spikes/model-switch-log.txt`. The file is gitignored, but it is plain-text prompt logging nobody needs any more.

Fix: delete the `jev-spike` mod (planned once the router works, and it does).

## Low

- **L1 DNS rebinding (reproduced).** The server answers a request with `Host: attacker.example`. A web page can rebind its own domain to 127.0.0.1; it is then same-origin and can set the custom header without a preflight, spending Jev credit and reading decisions. (A normal cross-site preflight is correctly refused: `OPTIONS` → 501.) Fix: only accept `Host: 127.0.0.1:8765` or `localhost:8765`.
- **L2 Non-object JSON crashes the thread (reproduced).** `["x"]` as the body → connection reset, because `body.get` runs outside the `try`. Fix: reject non-objects with 400.
- **L3 Negative Content-Length hangs (reproduced).** `Content-Length: -1` → `rfile.read(-1)` waits for EOF; no reply after 4 s. Local-only denial of service. Fix: reject negative or very large lengths.
- **L4 Subagent turns move the idle clock.** `turn.complete` also fires for subagent loops (the event carries `agentId`), so the mod can treat the main cache as warmer than it is. Fix: ignore events with an `agentId`.
- **L5 Virtual tier in advise mode.** `previousTier` is set to the router's choice even when nothing switched, so follow-up logic protects a tier you're not on. Fix: only remember the tier after a switch or when it matches the running model.
- **L6 `!heavy` alone.** The override regex needs whitespace after the tier, so `!heavy` on its own goes to Claude as text. Fix: `/^!(light|standard|heavy)(\s+|$)/i`.
- **L7 Global mode.** `/jev mode auto` in one session changes every session (`$.store` is cross-session). Probably fine, but it's surprising and isn't documented.
- **L8 Unbounded growth.** `router-log.jsonl`, `judge-log.jsonl` and `judge-cache.sqlite` grow forever. Belongs with the planned log-retention work (DESIGN 6.2).
- **L9 Lost plugin context.** The resend passes only `text`, not `e.context` that other plugins attached.
- **L10 `opusplan`.** `tier_of_model('opusplan')` is `heavy`, so a non-heavy decision would switch you off opusplan. Same fix as H1: hands off models the router doesn't manage.

## Not bugs, but worth knowing

- **Latency:** each prompt waits for a health check, a usage read and the Jev call, about 0.3–0.6 s in total.
- **Desktop model label:** after a mod-driven switch the app's model label kept showing the old model, while `$.session.model()` and the actual responses showed the new one. That's the app, not this code; `/jev` shows the truth.
- **TypeSafe console count:** the console showed 5 requests while our logs show 34 real calls. Request ids are now logged (`77268c6`) so the two can be matched. Unresolved until checked in the console.
- **Up-switches also cost a cache rebuild.** That's accepted by design for complex work, but on a 300k-token context it's a large one-off cost.

## Suggested fix order
1. H1 + L10 (hands off unknown models), H3 (no Haiku above 100k), M2 + M3 (advise instead of drop mid-turn or with mentions). All are small changes to `register.tsx` and `policy.py`.
2. H2 redaction, with tests.
3. M1 effort-only switching.
4. L1–L3 server input hardening.
5. M4 restart/version, M7 remove spike, L4–L6.
6. M5 portability and M6 mod tests, before publishing on GitHub.
