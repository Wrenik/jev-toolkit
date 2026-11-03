"""Cost arithmetic for "keep, compact or clear" (docs/CONTEXT_COMPILER.md section 4).

Pure functions, no I/O. Every price is a relative weight in input-token
equivalents, NOT dollars and NOT the Pro plan's real accounting (which is not
published). The defaults are an assumption borrowed from API list-price ratios;
see `Weights`.

Terms: C = tokens in the context now, S = size of a compaction summary,
n = number of user turns still to come in this task (the next one included).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Action = Literal["keep", "compact_now", "clear_with_seed"]
Closure = Literal["finished", "paused", "switch"]


@dataclass(frozen=True)
class Weights:
    """ASSUMED relative weights per token; calibrate against real usage before trusting totals."""

    read: float = 0.1  # r: cache read
    write: float = 2.0  # w: cache write (1 h TTL)
    out: float = 5.0  # o: output token


@dataclass(frozen=True)
class Policy:
    # Above this many tokens, compact regardless of the arithmetic (accuracy / hard limit).
    max_context_tokens: int = 700_000
    # Summary size as a fraction of C when the caller has no better estimate
    # (measured: auto compaction -89 %, manual -97 %, so 3-11 %; take the cautious end).
    summary_ratio: float = 0.11
    # Re-seed size for the clear option, in tokens.
    seed_tokens: int = 4_000
    # Compact only if the saving exceeds the cost by this factor (margin against noisy n).
    margin: float = 1.25
    # Clear when this little of the context is still needed (0..1) and the task is over or switched.
    clear_below_load_bearing: float = 0.25


@dataclass(frozen=True)
class Situation:
    context_tokens: int
    expected_turns: float
    cache_warm: bool = True
    summary_tokens: int | None = None
    # Both optional judgement inputs; None means "unknown", which never triggers a clear.
    load_bearing: float | None = None
    closure: Closure | None = None


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    cost_keep: float
    cost_compact: float
    cost_clear: float
    break_even_turns: float | None


def summary_size(s: Situation, policy: Policy) -> int:
    return s.summary_tokens if s.summary_tokens is not None else round(s.context_tokens * policy.summary_ratio)


def break_even_turns(context: int, summary: int, w: Weights = Weights()) -> float:
    """Turns after which compacting has paid for itself.

    Derivation (same warm or cold, the compact call just replaces the next turn's read):
    cost(compact) - cost(keep) = (o + w)*S - (n - 1)*r*(C - S), which is zero at
    n* = 1 + (o + w)*S / (r*(C - S)).  Returns inf when compacting cannot shrink the context.
    """
    if summary >= context:
        return float("inf")
    return 1 + (w.out + w.write) * summary / (w.read * (context - summary))


def total_costs(s: Situation, policy: Policy = Policy(), w: Weights = Weights()) -> tuple[float, float, float]:
    """(keep, compact, clear) over the next `expected_turns` turns, in weighted tokens."""
    c, n = s.context_tokens, max(s.expected_turns, 1.0)
    sm, seed = summary_size(s, policy), policy.seed_tokens
    first_read = w.read if s.cache_warm else w.write
    keep = first_read * c + (n - 1) * w.read * c
    compact = first_read * c + w.out * sm + w.write * sm + (n - 1) * w.read * sm
    clear = w.write * seed + (n - 1) * w.read * seed  # excludes the cost of what the clear forgets
    return keep, compact, clear


def decide(s: Situation, policy: Policy = Policy(), w: Weights = Weights()) -> Decision:
    keep, compact, clear = total_costs(s, policy, w)
    n_star = break_even_turns(s.context_tokens, summary_size(s, policy), w)

    def result(action: Action, reason: str) -> Decision:
        return Decision(action, reason, keep, compact, clear, None if n_star == float("inf") else n_star)

    if s.closure in ("finished", "switch") and s.load_bearing is not None \
            and s.load_bearing < policy.clear_below_load_bearing:
        return result("clear_with_seed", f"task {s.closure} and only {s.load_bearing:.0%} of the context is still needed")
    if s.context_tokens >= policy.max_context_tokens:
        return result("compact_now", f"context at or above the {policy.max_context_tokens}-token ceiling")
    if keep - compact > 0 and (keep - compact) >= (policy.margin - 1) * compact:
        return result("compact_now", f"saves {keep - compact:,.0f} weighted tokens over {s.expected_turns:.1f} turns (break-even {n_star:.1f})")
    return result("keep", f"compacting would not pay off within {s.expected_turns:.1f} turns (break-even {n_star:.1f})")
