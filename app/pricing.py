"""Money math. Pinned constants, integers only.

Unit: pico-USD (1e-12 USD). It is the unit that makes every per-token price a whole
number — $0.30 per million tokens is exactly 300,000 pico-USD per token — so a single
event's cost is exact and nothing is rounded until the rollup.

Prices are modeled on Gemini 2.5 Flash list prices and pinned here on purpose: a
billing system must not change what it charges because a web page changed.
"""

from __future__ import annotations

from dataclasses import dataclass

PICO_PER_USD = 10**12
PICO_PER_MICRO = 10**6   # micro-USD (1e-6 USD)
PICO_PER_CENT = 10**10

# --- pinned price list (pico-USD per unit) -----------------------------------
INPUT_PICO_PER_TOKEN = 300_000             # $0.30   / 1M fresh input tokens
CACHED_INPUT_PICO_PER_TOKEN = 75_000       # $0.075  / 1M cached input (25% of input)
OUTPUT_PICO_PER_TOKEN = 2_500_000          # $2.50   / 1M output tokens
REASONING_PICO_PER_TOKEN = OUTPUT_PICO_PER_TOKEN  # reasoning IS output: same price, by rule
API_CALL_PICO = 1_000_000_000              # $0.001  per billable API call


class InvalidUsage(ValueError):
    pass


@dataclass(frozen=True)
class TokenUsage:
    """Token counts as a provider reports them.

    input_tokens         total prompt tokens, INCLUDING the cached ones
    cached_input_tokens  the cached subset of input_tokens
    output_tokens        visible output, EXCLUDING reasoning
    reasoning_tokens     hidden thinking tokens, billed at the output rate
    """

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int

    def __post_init__(self) -> None:
        for name in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                raise InvalidUsage(f"{name} must be a non-negative integer")
        if self.cached_input_tokens > self.input_tokens:
            raise InvalidUsage("cached_input_tokens cannot exceed input_tokens (cached is a subset of input)")

    @property
    def fresh_input_tokens(self) -> int:
        return self.input_tokens - self.cached_input_tokens

    @property
    def quota_tokens(self) -> int:
        """What counts against the AI-token quota. Cached tokens are counted once,
        because they are already inside input_tokens."""
        return self.input_tokens + self.output_tokens + self.reasoning_tokens


def token_cost_pico(u: TokenUsage) -> int:
    """Exact cost of one AI call. Each category priced on its own — never summed first."""
    return (
        u.fresh_input_tokens * INPUT_PICO_PER_TOKEN
        + u.cached_input_tokens * CACHED_INPUT_PICO_PER_TOKEN
        + u.output_tokens * OUTPUT_PICO_PER_TOKEN
        + u.reasoning_tokens * REASONING_PICO_PER_TOKEN
    )


def api_call_cost_pico(calls: int) -> int:
    return calls * API_CALL_PICO


def round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounding .5 up. Money only ever rounds here."""
    if numerator < 0 or denominator <= 0:
        raise ValueError("round_half_up expects numerator >= 0 and denominator > 0")
    q, r = divmod(numerator, denominator)
    return q + 1 if r * 2 >= denominator else q


def pico_to_micro(pico: int) -> int:
    return round_half_up(pico, PICO_PER_MICRO)


def pico_to_cents(pico: int) -> int:
    return round_half_up(pico, PICO_PER_CENT)


def format_usd(pico: int) -> str:
    """'$0.014600' — micro-dollar precision, rounded once."""
    micro = pico_to_micro(pico)
    return f"${micro // 1_000_000}.{micro % 1_000_000:06d}"
