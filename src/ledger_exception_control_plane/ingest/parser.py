"""Syntax only: bytes to rows of text. Nothing here knows what a settlement line means.

The parser understands the wire format declared in ADR-031 and nothing else. It does not convert
a value, validate a currency, compare an amount or decide anything about a movement — every field
comes out as the string the file contained. Keeping that boundary sharp is what makes the
normaliser testable in isolation and stops "parse" quietly growing into "interpret".

**The column contract is declared here, not imported from the fixture package.** The fixtures
render this format and production reads it, and both must agree — but production code that
imported its format from the test corpus would be depending on its own test data. The two lists
are asserted equal by a test instead, which catches a drift in either direction without creating
the dependency.
"""

from __future__ import annotations

import csv
import dataclasses
import io
from typing import Final

from ledger_exception_control_plane.ingest.errors import Defect, QuarantineCode

#: The settlement file's columns, in order (ADR-031). The header must match this exactly.
#:
#: Exact order rather than a name-keyed lookup: this is a format the project defines, a deviation
#: is a format change, and a format change to a financial feed should stop the batch rather than
#: be absorbed silently. A reordered header is a different file from the one this parser was
#: written against, and it is cheaper to be told so than to discover it in a reconciliation.
SETTLEMENT_COLUMNS: Final[tuple[str, ...]] = (
    "psp_reference",
    "merchant_reference",
    "transaction_type",
    "amount",
    "currency",
    "value_date",
    "presentment_amount",
    "presentment_currency",
    "fx_rate",
    "memo",
)

#: The file is UTF-8. Stated rather than inherited: relying on the platform's preferred encoding
#: would make ingestion machine-dependent, which for a financial boundary is a defect and not a
#: convenience.
ENCODING: Final = "utf-8"


@dataclasses.dataclass(frozen=True, slots=True)
class ParsedRow:
    """One data row, as text. ``line`` is 1-based; the header is not a data row."""

    line: int
    fields: dict[str, str]


@dataclasses.dataclass(frozen=True, slots=True)
class ParseResult:
    """Rows that were readable, and defects for the file and the rows that were not.

    **Both can be non-empty at once, and both can be empty.** A file with one good row and one
    short row produces one of each; a header-only file produces neither. An earlier version of this
    docstring claimed "either rows or defects, never both, never neither", which was wrong in both
    directions and would have invited a caller to consume ``rows`` whenever it was non-empty.

    ``ok`` is therefore the only safe gate: rows from a file with *any* defect are discarded,
    because quarantine is batch-level (ADR-040). Read ``rows`` only when ``ok``.
    """

    rows: tuple[ParsedRow, ...]
    defects: tuple[Defect, ...]

    @property
    def ok(self) -> bool:
        """True when the whole file is admissible. The only condition under which ``rows`` may
        be used."""
        return not self.defects


def parse(payload: bytes) -> ParseResult:
    """Read the payload as the declared settlement format.

    Structural failure stops here and returns defects; it never raises. An ingestion boundary that
    raises on malformed input pushes the decision about what to do with an untrusted file into a
    ``try`` block somewhere else, and the whole point of this increment is that malformed input has
    a defined destination.
    """
    if not payload:
        return ParseResult((), (Defect(None, None, QuarantineCode.EMPTY_PAYLOAD),))

    try:
        text = payload.decode(ENCODING)
    except UnicodeDecodeError:
        # The exception carries byte offsets and a fragment of the payload. Neither is repeated:
        # the payload itself is retained, and the code says what to look for.
        return ParseResult((), (Defect(None, None, QuarantineCode.UNDECODABLE_PAYLOAD),))

    # A UTF-8 BOM would otherwise become part of the first header name, and the header check would
    # report a mismatch that is really an encoding artefact. Stripped explicitly so the diagnosis
    # is not misleading; nothing else about the bytes is touched, and the hash was taken before
    # this function ever ran.
    text = text.removeprefix("﻿")

    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), strict=True))
    except csv.Error:
        return ParseResult((), (Defect(None, None, QuarantineCode.MALFORMED_CSV),))

    if not rows:
        return ParseResult((), (Defect(None, None, QuarantineCode.EMPTY_PAYLOAD),))

    header, *data = rows
    if tuple(header) != SETTLEMENT_COLUMNS:
        # One code for a missing column, an extra column and a reordered header alike. They are
        # the same failure — the file is not this format — and splitting them would invite a
        # caller to treat one as recoverable.
        return ParseResult((), (Defect(0, None, QuarantineCode.HEADER_MISMATCH),))

    parsed: list[ParsedRow] = []
    defects: list[Defect] = []
    for offset, row in enumerate(data, start=1):
        # A trailing newline yields a final empty row from csv.reader; it is not a short row.
        if not row:
            continue
        if len(row) != len(SETTLEMENT_COLUMNS):
            defects.append(Defect(offset, None, QuarantineCode.ROW_FIELD_COUNT_MISMATCH))
            continue
        parsed.append(ParsedRow(offset, dict(zip(SETTLEMENT_COLUMNS, row, strict=True))))

    # Note what is *not* here: a header-only file parses to zero rows and no defects. A settlement
    # period with no movements is a real thing, and quarantining it would refuse a valid file on a
    # quiet day. A genuinely empty payload is a different case and is caught above.
    return ParseResult(tuple(parsed), tuple(defects))
