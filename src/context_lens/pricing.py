"""API list prices, used to put a dollar figure on token counts.

Subscription users don't pay per token, but the same tokens count against
their usage limits; dollars are the common unit for comparing before/after.
Prices as of 2026-10-06 (USD per million tokens). Cache writes cost 1.25x
input for the 5-minute TTL and 2x for the 1-hour TTL.
"""

from __future__ import annotations

PRICES_AS_OF = "2026-10-06"
WRITE_5M = 1.25
WRITE_1H = 2.0

# model id prefix -> (input, cache read, output)
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-1": (10.00, 0.25, 50.00),
    "claude-fable-5": (10.00, 1.00, 50.00),
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-opus-4": (5.00, 0.50, 25.00),
    "claude-sonnet-5-5": (2.00, 0.20, 10.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-sonnet-4": (3.00, 0.30, 15.00),
    "claude-haiku-5-5": (0.10, 0.01, 0.50),
    "claude-haiku-4": (1.00, 0.10, 5.00),
}


def price(model: str) -> tuple[float, float, float] | None:
    # Longest prefix wins so "claude-opus-5-5" never matches "claude-opus-5".
    for prefix in sorted(PRICES, key=len, reverse=True):
        if model.startswith(prefix):
            return PRICES[prefix]
    return None


def session_cost(model: str, inp: int, w5: int, w1: int, read: int, out: int) -> float | None:
    p = price(model)
    if p is None:
        return None
    pin, pread, pout = p
    return (inp * pin + w5 * pin * WRITE_5M + w1 * pin * WRITE_1H + read * pread + out * pout) / 1e6


def static_context_cost(model: str, tokens: int, turns: int, write_ttl_1h: bool = True) -> float | None:
    """What a fixed block of startup context costs over one session.

    Written to the cache once, then read back on every later turn.
    """
    p = price(model)
    if p is None or turns <= 0:
        return None
    pin, pread, _ = p
    write = pin * (WRITE_1H if write_ttl_1h else WRITE_5M)
    return tokens * (write + pread * (turns - 1)) / 1e6
