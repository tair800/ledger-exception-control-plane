"""Deliberately malformed artifacts, and the labelling that makes them safe to keep.

M2.1 has to quarantine structurally invalid input with a reason, and that cannot be tested
against well-formed data. These files exist so it can be.

**They are never loadable, and that is enforced rather than remembered.** Each one either
violates a database constraint or has no well-defined normalised form at all, so there is no
correct way to insert it. The loader consumes ``records.json``, which never references them; a
test asserts the intersection of the invalid paths and the loadable corpus is empty.

**They are constants, not generated.** A malformed file is malformed by construction, not by a
seed — varying the defect with the seed would make the quarantine tests that consume them
depend on which seed happened to be used.

Each defect is stated in :attr:`InvalidFixture.defect` as a *fact about the bytes*, not as the
quarantine reason M2.1 will emit. Inventing that vocabulary here would pre-empt an increment
this one does not own.
"""

from __future__ import annotations

import dataclasses
from typing import Final

from ledger_exception_control_plane.fixtures.schema import InvalidFixture


@dataclasses.dataclass(frozen=True, slots=True)
class _Invalid:
    record: InvalidFixture
    payload: bytes


def _csv(*rows: str) -> bytes:
    return ("\n".join(rows) + "\n").encode("utf-8")


_HEADER: Final = (
    "psp_reference,merchant_reference,transaction_type,amount,currency,value_date,"
    "presentment_amount,presentment_currency,fx_rate,memo"
)


INVALID_FIXTURES: Final[tuple[_Invalid, ...]] = (
    _Invalid(
        record=InvalidFixture(
            path="invalid/over-precise-amount.csv",
            defect="row 2 states an amount with five decimal places",
            why_it_exists=(
                "The M1.1 money contract rejects rather than rounds an over-precise value, and "
                "the rejection has to happen somewhere a user can see. This file is what proves "
                "the ingest path refuses it instead of quantising it on the way in."
            ),
        ),
        payload=_csv(
            _HEADER,
            "psp_0000000000a1,ORD-2026-100001,capture,120.12345,EUR,2026-06-03,,,,over-precise",
        ),
    ),
    _Invalid(
        record=InvalidFixture(
            path="invalid/missing-column.csv",
            defect="the header omits the currency column and every row is one field short",
            why_it_exists=(
                "A structurally invalid batch must quarantine with a reason (FR-2) rather than "
                "raise an index error deep in a parser."
            ),
        ),
        payload=_csv(
            "psp_reference,merchant_reference,transaction_type,amount,value_date,memo",
            "psp_0000000000b2,ORD-2026-100002,capture,45.00,2026-06-03,no currency column",
        ),
    ),
    _Invalid(
        record=InvalidFixture(
            path="invalid/bad-currency.csv",
            defect="row 2 carries a lower-case currency code and row 3 carries a four-letter one",
            why_it_exists=(
                "The database rejects both — currency is checked against ^[A-Z]{3}$ — so the "
                "ingest path must catch them first and say why, rather than surfacing a "
                "constraint violation to a user."
            ),
        ),
        payload=_csv(
            _HEADER,
            "psp_0000000000c3,ORD-2026-100003,capture,80.00,eur,2026-06-04,,,,lower case code",
            "psp_0000000000c4,ORD-2026-100004,capture,90.00,EURO,2026-06-04,,,,four letters",
        ),
    ),
    _Invalid(
        record=InvalidFixture(
            path="invalid/unparseable-amount.csv",
            defect="row 2 states an amount that is not a number and row 3 leaves it empty",
            why_it_exists=(
                "A line whose amount cannot be read has no normalised form at all. It is the "
                "case that must never reach a Decimal constructor unguarded, and never be "
                "defaulted to zero — a silent zero is a financial error, not a fallback."
            ),
        ),
        payload=_csv(
            _HEADER,
            "psp_0000000000d5,ORD-2026-100005,capture,not-a-number,EUR,2026-06-05,,,,unreadable",
            "psp_0000000000d6,ORD-2026-100006,capture,,EUR,2026-06-05,,,,empty amount",
        ),
    ),
)
