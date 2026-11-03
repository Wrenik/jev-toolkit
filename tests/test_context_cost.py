import math

import pytest

from jev_context.cost import Policy, Situation, Weights, break_even_turns, decide, total_costs


def test_break_even_worked_numbers():
    # Doc section 4.2 (it counts turns after the first, so n* = doc value + 1).
    assert break_even_turns(263_000, 20_000) == pytest.approx(1 + 140_000 / 24_300, rel=1e-6)
    assert break_even_turns(569_000, 20_000) == pytest.approx(1 + 140_000 / 54_900, rel=1e-6)
    assert break_even_turns(60_000, 20_000) == pytest.approx(1 + 140_000 / 4_000, rel=1e-6)


def test_cannot_shrink():
    assert math.isinf(break_even_turns(10_000, 10_000))


@pytest.mark.parametrize("warm", [True, False])
def test_difference_matches_formula_warm_or_cold(warm):
    w, c, s_tok = Weights(), 300_000, 30_000
    for n in (1, 3, 10):
        s = Situation(c, n, cache_warm=warm, summary_tokens=s_tok)
        keep, compact, _ = total_costs(s, Policy(), w)
        expected = (w.out + w.write) * s_tok - (n - 1) * w.read * (c - s_tok)
        assert compact - keep == pytest.approx(expected)


def test_total_costs_hand_computed():
    # warm, C=100k, S=10k, n=2: keep = 2*0.1*100k = 20k; compact = 10k + 50k + 20k + 1k = 81k
    keep, compact, clear = total_costs(Situation(100_000, 2, summary_tokens=10_000), Policy(seed_tokens=1000))
    assert keep == pytest.approx(20_000)
    assert compact == pytest.approx(81_000)
    assert clear == pytest.approx(2_000 + 100)


def test_small_context_is_left_alone():
    assert decide(Situation(60_000, 5)).action == "keep"


def test_large_context_long_task_compacts():
    d = decide(Situation(400_000, 30))
    assert d.action == "compact_now" and d.break_even_turns < 30


def test_large_context_short_task_keeps():
    assert decide(Situation(400_000, 2)).action == "keep"


def test_ceiling_overrides_arithmetic():
    assert decide(Situation(750_000, 1)).action == "compact_now"


def test_clear_needs_both_closure_and_low_need():
    assert decide(Situation(400_000, 1, closure="finished", load_bearing=0.1)).action == "clear_with_seed"
    assert decide(Situation(400_000, 1, closure="finished", load_bearing=0.9)).action == "keep"
    assert decide(Situation(400_000, 1, closure="paused", load_bearing=0.1)).action == "keep"
    # Unknown judgement never clears.
    assert decide(Situation(400_000, 1, closure="finished")).action == "keep"


def test_margin_requires_clear_win():
    # Just past break-even is not enough with the default margin.
    n = break_even_turns(300_000, round(300_000 * 0.11)) + 0.1
    assert decide(Situation(300_000, n)).action == "keep"
