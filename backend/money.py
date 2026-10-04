"""Money helpers.

Money is compared and persisted in integer minor units (cents), never as
floats. Comparing dollars with ``<=`` can be wrong at the boundary - ``150.00``
is not exactly representable, so a fee that should pass a $150.00 limit can
fail it. Converting to cents first makes the comparison exact (playbook layer
3: "money stored as integer minor units, never floats").
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

_CENTS = Decimal("0.01")


def to_cents(amount) -> int:
    """Convert a dollar amount to integer cents, rounding half up."""
    if amount is None:
        raise ValueError("amount is required")
    value = Decimal(str(amount)).quantize(_CENTS, rounding=ROUND_HALF_UP)
    return int(value * 100)


def from_cents(cents: int) -> float:
    return float(Decimal(int(cents)) / 100)


def format_cents(cents: int) -> str:
    return f"${int(cents) / 100:.2f}"
