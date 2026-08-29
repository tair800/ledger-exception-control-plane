"""Monetary construction for fixtures — exact by construction, never rounded.

The corpus must not be able to produce a value the M1.1 persistence contract would reject,
and it must not produce one by *rounding* something that was wrong. Both are avoided the same
way: amounts are built from an integer number of minor units and a currency's declared minor
digits, then rendered to a string and parsed as ``Decimal``. No division, no ``float``, no
``quantize`` on a value that was already wrong.

``Decimal(str)`` rather than ``Decimal(int) / Decimal(10**n)`` is deliberate. Division through
``Decimal`` obeys the ambient context precision and can produce a value with more digits than
intended; string construction is exact and carries exactly the scale written.
"""

from __future__ import annotations

import decimal
from typing import Final, NamedTuple

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
)


class Currency(NamedTuple):
    """An ISO 4217 code and the number of digits its minor unit actually uses."""

    code: str
    minor_digits: int


#: The currencies the corpus uses. Chosen to span the minor-unit range that matters: a
#: zero-digit currency, the common two-digit case, and a three-digit one. All are real ISO
#: 4217 codes.
#:
#: The four-digit ceiling ``MONEY_MAX_SCALE`` allows is deliberately **not** exercised by
#: fabricated settlement data — the currencies that use four minor digits (CLF, UYW) are units
#: of account that would not plausibly appear in a card-settlement feed, and inventing one
#: would make the corpus less realistic in order to satisfy a boundary that the M1.1 schema
#: tests already cover directly against PostgreSQL.
EUR: Final = Currency("EUR", 2)
USD: Final = Currency("USD", 2)
GBP: Final = Currency("GBP", 2)
JPY: Final = Currency("JPY", 0)
BHD: Final = Currency("BHD", 3)

CURRENCIES: Final[tuple[Currency, ...]] = (EUR, USD, GBP, JPY, BHD)

BY_CODE: Final[dict[str, Currency]] = {c.code: c for c in CURRENCIES}


def money(minor_units: int, currency: Currency) -> decimal.Decimal:
    """Build an exact amount from a signed integer count of minor units.

    ``money(-12345, EUR)`` is ``Decimal("-123.45")``. ``money(-12345, JPY)`` is
    ``Decimal("-12345")`` — a zero-digit currency has no fractional part at all, and writing
    one would be a fiction about the currency.
    """
    if currency.minor_digits > MONEY_MAX_SCALE:
        raise ValueError(f"{currency.code} needs more precision than the schema permits")

    sign = "-" if minor_units < 0 else ""
    digits = str(abs(minor_units))

    if currency.minor_digits == 0:
        rendered = f"{sign}{digits}"
    else:
        padded = digits.rjust(currency.minor_digits + 1, "0")
        whole, fraction = padded[: -currency.minor_digits], padded[-currency.minor_digits :]
        rendered = f"{sign}{whole}.{fraction}"

    value = decimal.Decimal(rendered)
    if abs(value) >= MONEY_MAGNITUDE_EXCLUSIVE_BOUND:
        raise ValueError("amount exceeds the schema's magnitude bound")
    return value


def render(value: decimal.Decimal) -> str:
    """Render an amount for a settlement or ledger file.

    ``str`` on a ``Decimal`` preserves the scale it was constructed with and never switches to
    exponent notation for the magnitudes this corpus produces, so the rendered file carries the
    currency's real precision rather than a normalised one.
    """
    return str(value)
