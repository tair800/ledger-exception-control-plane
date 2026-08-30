"""Text to typed values — the canonical settlement line FR-2 requires.

Deterministic and total: the same row yields the same result, and every row yields either a
:class:`NormalisedLine` or a list of :class:`Defect`. No clock, no locale, no randomness, no I/O.

**Money never touches a float.** Values are built with ``Decimal(text)`` straight from the string
the file contained, after a regex that admits only a plain signed decimal. ``float(text)`` would
lose exactness before the value was ever examined, and ``Decimal(float_value)`` would preserve the
loss with false precision — the M1.1 correction exists because a *rounding* at a boundary is
undetectable afterwards. An over-precise amount is **rejected**, never quantised: ADR-020 records
that quantising to make a value fit is the defect, not the fix.

**Every value accepted here must be storable.** The rule is not "looks reasonable" but "the
destination column can hold it": a field this module accepts and the database then refuses would
turn a quarantine into an unhandled error, and — because the receipt is already committed — would
jam that batch permanently. See :data:`_UNSTORABLE`.

**Nothing here canonicalises a reference.** No case folding, no punctuation stripping, no internal
whitespace collapsing. Those are matching decisions — how close two references have to be before
they denote the same movement — and M2.2 owns them. Doing it here would bake a matching rule into
the persisted record where no test could later vary it, and would destroy the difference between
two references that a matcher may need to see. See ADR-039.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import re
from typing import Final

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
)
from ledger_exception_control_plane.ingest.errors import Defect, QuarantineCode
from ledger_exception_control_plane.ingest.parser import ParsedRow

#: A plain signed decimal, and only that.
#:
#: Deliberately narrower than ``Decimal`` accepts. ``Decimal("NaN")``, ``Decimal("Infinity")`` and
#: ``Decimal("1E+3")`` all construct successfully, and the first two would then have to be caught
#: downstream by a check that is easy to forget — the schema's own money constraint had exactly
#: that gap until it was found and fixed (ADR-020). Excluding them at the syntax boundary means
#: there is no second place to remember.
_DECIMAL_TEXT: Final = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")

#: ISO 8601 calendar date, and only that. ``date.fromisoformat`` alone would accept forms the
#: settlement format does not declare, and ``strptime`` with a locale-dependent directive would
#: make ingestion depend on the machine.
_ISO_DATE: Final = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

#: ISO 4217 alphabetic code. Same rule the database enforces, applied early so a rejection names
#: the column rather than surfacing a constraint violation.
_CURRENCY: Final = re.compile(r"^[A-Z]{3}$")

#: Column limits, mirroring ``settlement_line``. Declared rather than read from the ORM so this
#: module does not depend on the mapper to validate; a test asserts the two agree, which catches a
#: drift in either direction.
MAX_PSP_REFERENCE: Final = 128
MAX_MERCHANT_REFERENCE: Final = 128

#: Characters a persisted text field may not contain.
#:
#: U+0000 is the one that matters and it is not a nicety: PostgreSQL cannot represent a NUL in a
#: character type, so a reference containing one normalises cleanly and then fails the INSERT with
#: SQLSTATE 22021. That turns a quarantine into an unhandled error — and because the receipt is
#: already committed and the payload is immutable, every re-delivery reproduces it, so the batch
#: can never reach ``parsed`` or ``quarantined``. A file that permanently jams its own ingestion is
#: the worst failure this boundary can have, and it was reachable from a single byte.
#:
#: The other C0 controls are rejected with it. They are storable, but a control character in a
#: settlement reference is malformed under any reading, and they are precisely the characters that
#: corrupt a downstream log line, CSV export or terminal.
_UNSTORABLE = re.compile(r"[\x00-\x1f\x7f]")


@dataclasses.dataclass(frozen=True, slots=True)
class NormalisedLine:
    """One settlement movement, typed.

    The first six fields are what ``settlement_line`` persists. The rest are declared by the file
    format (ADR-031) and have no column at M1: they are carried here because FR-2 asks for a typed
    representation of the *line*, not of the subset that happens to be stored, and they remain
    available in the immutable raw payload for the increments that need them. Inventing columns
    for them here would be adding schema this increment was not asked for.
    """

    line_number: int
    psp_reference: str
    merchant_reference: str | None
    amount: decimal.Decimal
    currency: str
    value_date: dt.date

    transaction_type: str
    presentment_amount: decimal.Decimal | None
    presentment_currency: str | None
    fx_rate: decimal.Decimal | None
    memo: str | None


def _absent(value: str) -> bool:
    """Empty and whitespace-only both mean "the file did not supply this".

    The one transformation this module applies to a text field, and it is a *reading* rather than
    a rewrite: no character inside a supplied value is altered. A field of three spaces is not a
    reference, and treating it as one would put an unmatchable value into a financial record.
    """
    return not value.strip()


def _decimal(
    text: str, line: int, column: str, code: QuarantineCode
) -> tuple[decimal.Decimal | None, Defect | None]:
    if not _DECIMAL_TEXT.fullmatch(text):
        return None, Defect(line, column, code)
    # Exact, from the text. No float has been involved at any point.
    return decimal.Decimal(text), None


def _money(text: str, line: int, column: str) -> tuple[decimal.Decimal | None, Defect | None]:
    """A monetary value under the M1.1 contract, or the defect that refuses it."""
    value, defect = _decimal(text, line, column, QuarantineCode.INVALID_AMOUNT)
    if defect is not None or value is None:
        return None, defect

    # Value-based, not representation-based, and the distinction is the whole of ADR-020.
    #
    # The first version compared the number of digits after the point, which rejects
    # ``120.450000`` — a value the column stores exactly and that ADR-020's own verification
    # table lists as accepted. That is a false quarantine: an entire settlement batch refused
    # because a PSP's renderer emits a fixed six-decimal scale. Worse, the rationale did not
    # even hold internally, because leading zeros were being canonicalised silently on the same
    # path while trailing ones condemned the file.
    #
    # This mirrors the column's ``trunc(amount, 4) = amount``: scale by 10^4 and require the
    # result to be a whole number, so a value is refused exactly when four decimal places cannot
    # hold it. ``1.230000`` passes; ``1.23456`` does not.
    scaled = value.scaleb(MONEY_MAX_SCALE)
    if scaled != scaled.to_integral_value(rounding=decimal.ROUND_DOWN):
        # Rejected, never rounded to fit.
        return None, Defect(line, column, QuarantineCode.AMOUNT_PRECISION_EXCEEDED)
    if abs(value) >= MONEY_MAGNITUDE_EXCLUSIVE_BOUND:
        return None, Defect(line, column, QuarantineCode.AMOUNT_OUT_OF_RANGE)
    return value, None


def normalise(row: ParsedRow) -> tuple[NormalisedLine | None, tuple[Defect, ...]]:
    """Convert one parsed row into a canonical line, or report why it cannot be.

    Every field is validated before returning, so a row with three problems reports three defects
    rather than making an operator fix them one delivery at a time.
    """
    defects: list[Defect] = []
    line = row.line
    field = row.fields

    psp_reference = field["psp_reference"]
    if _absent(psp_reference):
        defects.append(Defect(line, "psp_reference", QuarantineCode.MISSING_REQUIRED_FIELD))
    elif len(psp_reference) > MAX_PSP_REFERENCE:
        defects.append(Defect(line, "psp_reference", QuarantineCode.FIELD_TOO_LONG))
    elif _UNSTORABLE.search(psp_reference):
        defects.append(Defect(line, "psp_reference", QuarantineCode.UNSTORABLE_CHARACTER))

    merchant_reference: str | None = None
    if not _absent(field["merchant_reference"]):
        merchant_reference = field["merchant_reference"]
        if len(merchant_reference) > MAX_MERCHANT_REFERENCE:
            defects.append(Defect(line, "merchant_reference", QuarantineCode.FIELD_TOO_LONG))
        elif _UNSTORABLE.search(merchant_reference):
            defects.append(Defect(line, "merchant_reference", QuarantineCode.UNSTORABLE_CHARACTER))

    amount, defect = _money(field["amount"], line, "amount")
    if defect is not None:
        defects.append(defect)

    currency = field["currency"]
    if not _CURRENCY.fullmatch(currency):
        defects.append(Defect(line, "currency", QuarantineCode.INVALID_CURRENCY))

    value_date: dt.date | None = None
    raw_date = field["value_date"]
    if not _ISO_DATE.fullmatch(raw_date):
        defects.append(Defect(line, "value_date", QuarantineCode.INVALID_DATE))
    else:
        try:
            value_date = dt.date.fromisoformat(raw_date)
        except ValueError:
            # Shape is right, the date is not: 2026-13-01, 2026-02-30.
            defects.append(Defect(line, "value_date", QuarantineCode.INVALID_DATE))

    presentment_amount, presentment_currency, fx_rate = _normalise_presentment(row, defects)

    if defects:
        return None, tuple(defects)

    # Every branch above either produced a value or appended a defect, so reaching here means all
    # three are populated. Asserted rather than silently coerced.
    assert amount is not None
    assert value_date is not None
    return (
        NormalisedLine(
            line_number=line,
            psp_reference=psp_reference,
            merchant_reference=merchant_reference,
            amount=amount,
            currency=currency,
            value_date=value_date,
            transaction_type=field["transaction_type"],
            presentment_amount=presentment_amount,
            presentment_currency=presentment_currency,
            fx_rate=fx_rate,
            memo=None if _absent(field["memo"]) else field["memo"],
        ),
        (),
    )


def _normalise_presentment(
    row: ParsedRow, defects: list[Defect]
) -> tuple[decimal.Decimal | None, str | None, decimal.Decimal | None]:
    """The cross-currency columns: validated for coherence, never converted.

    §3 lists a currency-conversion policy engine as a non-goal and §13 says rates arrive as
    recorded inputs. So the rate is checked for shape and sign and then carried; no arithmetic is
    performed on it here or anywhere in this package. What *is* enforced is that the three columns
    tell a consistent story, because a presented amount with no currency is a number with no unit
    — the same defect the database's own pairing constraint exists to prevent.
    """
    line = row.line
    field = row.fields
    has_amount = not _absent(field["presentment_amount"])
    has_currency = not _absent(field["presentment_currency"])
    has_rate = not _absent(field["fx_rate"])

    if has_amount != has_currency:
        defects.append(
            Defect(line, "presentment_amount", QuarantineCode.INCOHERENT_PRESENTMENT_FIELDS)
        )
    if has_rate and not has_currency:
        # A rate with nothing to convert from is not interpretable, and silently ignoring it would
        # discard the only evidence that the file believed this was a cross-currency movement.
        defects.append(Defect(line, "fx_rate", QuarantineCode.INCOHERENT_PRESENTMENT_FIELDS))

    presentment_amount: decimal.Decimal | None = None
    if has_amount:
        presentment_amount, defect = _money(field["presentment_amount"], line, "presentment_amount")
        if defect is not None:
            defects.append(defect)

    presentment_currency: str | None = None
    if has_currency:
        presentment_currency = field["presentment_currency"]
        if not _CURRENCY.fullmatch(presentment_currency):
            defects.append(Defect(line, "presentment_currency", QuarantineCode.INVALID_CURRENCY))

    fx_rate: decimal.Decimal | None = None
    if has_rate:
        fx_rate, defect = _decimal(
            field["fx_rate"], line, "fx_rate", QuarantineCode.INVALID_FX_RATE
        )
        if defect is not None:
            defects.append(defect)
        elif fx_rate is not None and fx_rate <= 0:
            # A rate is not money and carries no four-decimal ceiling, but a non-positive one is
            # not a rate.
            defects.append(Defect(line, "fx_rate", QuarantineCode.INVALID_FX_RATE))

    return presentment_amount, presentment_currency, fx_rate
