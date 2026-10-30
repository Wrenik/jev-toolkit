# jev-toolkit

Small tools that put a **judgement model** next to a generative LLM. The LLM writes; the judgement model answers typed questions (yes/no, pick one, rate on a scale) with probabilities; ordinary code acts on those answers. The first judge supported is [TypeSafe Jev](https://docs.typesafe.ai).

This is a personal learning project (IT security now, data science next) built in public. It is early, and this README says plainly what works and what doesn't.

## Status

| Part | State |
|---|---|
| `jev_core`: judge interface, Jev adapter, fake judge, cache, decision log | **Working, unit-tested offline.** Live call not yet run. |
| Model/effort router for Claude Code | Next. Spike done: a Claude Code mod can switch the main model for the *next* prompt and call a small model itself. |
| Context compiler, injection scanner, agent monitor, fact checker, source restriction | Designed in [docs/DESIGN.md](docs/DESIGN.md), not built. |

## Why a judgement model

A generative LLM is a poor referee of its own work. A model that returns *typed answers with probabilities* gives code something it can compare against a threshold, log, and audit. Jev is built for exactly that, and it is cheap: about $0.042 per million input tokens, output free (October 2026 price).

## Privacy: read this before using it

Everything passed to the judge as `state` is **sent to TypeSafe's servers**. Their documentation (checked 2026-10-30) does not say where that data is processed. TypeSafe states it does not train on user data.

Jev is used because, as of today, it is the only model of its kind on the market, not because sending prompts to a US company is the goal. **The plan is to replace it with a local model or a European provider.** That is why every tool depends on the `Judge` interface in `jev_core`, never on the TypeSafe SDK directly: a replacement is one new adapter module. Until then:

- Don't send secrets, credentials or other people's personal data as `state`.
- The decision log stores a hash and the length of each state, not the text (unless you turn that on for local debugging).

## Built for Claude Code on a subscription

The tools are meant to work with a Claude Pro/Max **subscription inside Claude Code**, not with pay-per-token API keys. They hook into Claude Code's own extension points (mods, hooks, MCP tools). The judge side is model-independent; the generative side is whatever Claude Code session (or later, local LLM) you already run.

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Windows; use .venv/bin/python elsewhere
cp .env.example .env                                  # then paste your key into .env
.venv/Scripts/python -m jev_core check                # shows whether the key is found (never prints it)
.venv/Scripts/python -m jev_core smoke                # one real Jev call, a fraction of a cent
.venv/Scripts/python -m pytest                        # offline tests, no key needed
```

No key? Set `JEV_JUDGE=fake` in `.env` and everything runs offline with a neutral fake judge.

## Using `jev_core`

```python
from jev_core import Pick, Rate, YesNo, default_judge

judge = default_judge(tag="example")   # Jev, wrapped in a cache and a JSONL decision log
verdict = judge.ask(
    "Rename the variable cnt to count in utils.py.",
    {
        "mechanical": YesNo("Is this a small mechanical edit with no design decisions?"),
        "risk": Pick("What is the risk?", {"none": None, "low": None, "high": "could delete data"}),
        "complexity": Rate("How complex is this?", ["trivial", "small", "medium", "large"]),
    },
)
verdict["mechanical"].p_yes         # probability of "yes", 0..1
verdict["risk"].probabilities       # {"none": ..., "low": ..., "high": ...}
verdict["complexity"].level         # most probable level
verdict.cost_usd, verdict.cached
```

Ask several questions in one call: you pay for the state's tokens once.

Errors come in two kinds so each tool can choose how to fail: `JudgeUnavailable` (network, timeout, rate limit; try later) and `JudgeConfigError` (bad key or request; fix the setup). Routers should fail **open** (do nothing); security tools should fail **closed** (block).

## Layout

```
src/jev_core/
  types.py    questions and answers; no TypeSafe imports
  judge.py    the Judge interface and errors
  jev.py      the only module that talks to TypeSafe
  fake.py     offline judge for tests
  cache.py    SQLite cache keyed on judge, model version, state and questions
  log.py      one JSON line per call
  config.py   .env loading and the default judge stack
docs/DESIGN.md  design of all planned tools
tests/          offline tests (HTTP mocked with the documented response shape)
```

## Known limits

- Only text questions (yes/no, pick, rate). Jev's image `box` questions aren't wrapped.
- The TypeSafe SDK is pre-1.0 and has changed its question types between minor versions; it is pinned to `0.7.5`, and the model to `jev-1.14.0`.
- Cost figures are estimates from the list price in `types.py`, not from billing.
- The tests check the mapping against the response shape in TypeSafe's docs; they don't prove the live API still matches.

## Prior art

Ideas this project is related to. No code from them is used.

- [Instructor](https://github.com/567-labs/instructor) and [Outlines](https://github.com/dottxt-ai/outlines): structured, typed output *from generative LLMs*. Here the typed answers come from a separate judgement model, so the referee is not the writer.
- [RouteLLM](https://github.com/lm-sys/RouteLLM): learned routing between a strong and a weak LLM. The router planned here instead asks a judge typed questions, logs each decision with probabilities, and runs inside Claude Code on a subscription.
- [LiteLLM](https://github.com/BerriAI/litellm): one interface to many LLM providers. `jev_core` is the same idea at a much smaller scale, for judges rather than generators.

## License

To be decided before the repository is made public.
