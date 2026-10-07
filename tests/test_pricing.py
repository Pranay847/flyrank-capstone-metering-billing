"""Money math. Pure functions, exact integers, no database."""

import pytest

from app import pricing as P
from app.pricing import InvalidUsage, TokenUsage, token_cost_pico


def test_pinned_price_constants():
    # Pinned on purpose. If a price changes, this test must change with it, visibly.
    assert P.INPUT_PICO_PER_TOKEN == 300_000            # $0.30 / 1M
    assert P.CACHED_INPUT_PICO_PER_TOKEN == 75_000      # $0.075 / 1M
    assert P.OUTPUT_PICO_PER_TOKEN == 2_500_000         # $2.50 / 1M
    assert P.REASONING_PICO_PER_TOKEN == P.OUTPUT_PICO_PER_TOKEN
    assert P.API_CALL_PICO == 1_000_000_000             # $0.001 / call


def test_worked_example_exact_total():
    # 10,000 input of which 4,000 cached; 2,000 output; 3,000 reasoning.
    u = TokenUsage(input_tokens=10_000, cached_input_tokens=4_000,
                   output_tokens=2_000, reasoning_tokens=3_000)
    #   fresh   6,000 x   300,000 =  1,800,000,000
    #   cached  4,000 x    75,000 =    300,000,000
    #   out+rsn 5,000 x 2,500,000 = 12,500,000,000
    assert token_cost_pico(u) == 14_600_000_000
    assert P.format_usd(token_cost_pico(u)) == "$0.014600"
    assert u.quota_tokens == 15_000  # cached counted once, inside input


def test_reasoning_tokens_are_billed_as_output():
    assert token_cost_pico(TokenUsage(0, 0, 0, 1_000)) == token_cost_pico(TokenUsage(0, 0, 1_000, 0))
    assert token_cost_pico(TokenUsage(0, 0, 0, 1_000)) == 2_500_000_000


def test_cached_input_is_cheaper_than_fresh_input():
    all_cached = token_cost_pico(TokenUsage(1_000, 1_000, 0, 0))
    all_fresh = token_cost_pico(TokenUsage(1_000, 0, 0, 0))
    assert all_cached == 75_000_000 and all_fresh == 300_000_000
    assert all_cached * 4 == all_fresh


def test_categories_cannot_simply_be_added():
    u = TokenUsage(10_000, 4_000, 2_000, 3_000)
    naive_count = u.input_tokens + u.cached_input_tokens + u.output_tokens + u.reasoning_tokens
    naive_cost = naive_count * P.INPUT_PICO_PER_TOKEN
    assert naive_count == 19_000 != u.quota_tokens          # double-counts the cache
    assert naive_cost == 5_700_000_000 != token_cost_pico(u)  # wrong by $0.0089


def test_cached_cannot_exceed_input():
    with pytest.raises(InvalidUsage):
        TokenUsage(input_tokens=10, cached_input_tokens=11, output_tokens=0, reasoning_tokens=0)


@pytest.mark.parametrize("bad", [-1, 1.5, True, "5"])
def test_token_counts_must_be_non_negative_ints(bad):
    with pytest.raises(InvalidUsage):
        TokenUsage(input_tokens=bad, cached_input_tokens=0, output_tokens=0, reasoning_tokens=0)


def test_rounding_happens_once_at_the_rollup_not_per_event():
    one_token = token_cost_pico(TokenUsage(1, 0, 0, 0))     # 0.3 micro-USD
    assert one_token == 300_000
    per_event_rounded = sum(P.pico_to_micro(one_token) for _ in range(1_000))
    exact_then_rounded = P.pico_to_micro(one_token * 1_000)
    assert per_event_rounded == 0      # the bug: 1,000 calls billed as free
    assert exact_then_rounded == 300   # correct: 300 micro-USD


def test_round_half_up():
    assert P.round_half_up(4, 10) == 0
    assert P.round_half_up(5, 10) == 1
    assert P.round_half_up(15, 10) == 2
    assert P.pico_to_cents(5_000_000_000) == 1  # half a cent rounds up


@pytest.mark.parametrize("num,den", [(-1, 10), (5, 0), (5, -10)])
def test_round_half_up_rejects_bad_inputs(num, den):
    with pytest.raises(ValueError):
        P.round_half_up(num, den)


def test_api_call_cost_is_linear_per_call():
    assert P.api_call_cost_pico(0) == 0
    assert P.api_call_cost_pico(1_000) == P.PICO_PER_USD   # 1,000 calls = $1.00


def test_format_usd_carries_whole_dollars_and_rounds_half_up():
    assert P.format_usd(0) == "$0.000000"
    assert P.format_usd(12 * P.PICO_PER_USD + 345_678 * P.PICO_PER_MICRO) == "$12.345678"
    assert P.format_usd(P.PICO_PER_MICRO // 2) == "$0.000001"       # half a micro rounds up
    assert P.format_usd(P.PICO_PER_MICRO // 2 - 1) == "$0.000000"
    assert P.format_usd(P.PICO_PER_USD - 1) == "$1.000000"          # carries into the dollar
