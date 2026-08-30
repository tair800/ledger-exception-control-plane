"""The quarantine vocabulary — closed, bounded, and safe to store.

`PROJECT_SPEC.md` FR-2 requires an invalid batch to be quarantined **with a reason**, and that
reason lands in a financial control record that an operator reads and an auditor may later
read. Three properties follow, and none of them is free:

* **Closed.** A reason is a code from :class:`QuarantineCode`, not prose. An operator can build
  a runbook against a closed set; they cannot against whatever string a library happened to
  raise.
* **Bounded.** The rendered reason has a hard length cap and a character allowlist, both
  asserted. A reason column that can hold an arbitrary parser message is a place for a stack
  trace to end up.
* **Free of input and of internals.** The reason names the code, the line and the column — never
  the offending value, never an exception message. The offending value is already durably stored
  in ``raw_payload``, so repeating it here buys nothing and risks putting payload content
  somewhere it was not meant to travel. `PROJECT_SPEC.md` §17 treats a stored string as a log
  line waiting to happen.

This is why the ingestion boundary validates explicitly rather than through Pydantic, despite
NFR-2's general rule. Pydantic's error strings are neither a closed vocabulary nor bounded, and
they interpolate the offending input by design — see ADR-038.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from typing import Final

#: Hard ceiling on a rendered reason. Not a guess: the cap exists so the reason cannot grow with
#: the input, which is the property that keeps a parser message out of the database.
MAX_REASON_LENGTH: Final = 512

#: Everything a rendered reason is allowed to contain. Asserted rather than assumed, so a future
#: code carrying a stray character fails a test rather than reaching a control record.
REASON_ALLOWED = re.compile(r"^[A-Z0-9_a-z ,;:+()\-]*$")

#: Appended when a reason has to be cut. Inside :data:`REASON_ALLOWED` by construction.
_TRUNCATED: Final = " (truncated)"

#: Defects reported per batch before the list is truncated. Reporting only the first would make
#: fixing a file an iterative guessing game; reporting all of them would let the reason grow with
#: the input, which is the thing the cap exists to prevent.
MAX_REPORTED_DEFECTS: Final = 5


class QuarantineCode(enum.StrEnum):
    """Why a batch was refused. Structural codes first, then field-level ones.

    Every value is actionable on its own: it names a class of defect an operator can look for in
    the retained payload. None of them is a catch-all — there is deliberately no ``UNKNOWN``,
    because a code that means "something went wrong" is prose wearing an enum's clothes.
    """

    # Structural — the file could not be read as the declared format at all.
    EMPTY_PAYLOAD = "empty_payload"
    UNDECODABLE_PAYLOAD = "undecodable_payload"
    MALFORMED_CSV = "malformed_csv"
    HEADER_MISMATCH = "header_mismatch"
    ROW_FIELD_COUNT_MISMATCH = "row_field_count_mismatch"

    # Field-level — the file parsed, but a value is not admissible.
    MISSING_REQUIRED_FIELD = "missing_required_field"
    FIELD_TOO_LONG = "field_too_long"
    UNSTORABLE_CHARACTER = "unstorable_character"
    INVALID_AMOUNT = "invalid_amount"
    AMOUNT_PRECISION_EXCEEDED = "amount_precision_exceeded"
    AMOUNT_OUT_OF_RANGE = "amount_out_of_range"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_DATE = "invalid_date"
    INVALID_FX_RATE = "invalid_fx_rate"
    INCOHERENT_PRESENTMENT_FIELDS = "incoherent_presentment_fields"


@dataclasses.dataclass(frozen=True, slots=True, order=True)
class Defect:
    """One reason a batch cannot be accepted.

    ``line`` is the 1-based data row, counting the header as row 0 — the number a person sees in
    a spreadsheet minus one, and the number that becomes ``settlement_line.line_number`` for the
    rows that are valid. ``None`` for a defect in the file as a whole.

    Ordered, so a batch's defects are reported in a deterministic sequence rather than in
    whatever order validation happened to run.
    """

    line: int | None
    column: str | None
    code: QuarantineCode

    def render(self) -> str:
        parts = [self.code.value]
        if self.line is not None:
            parts.append(f"line {self.line}")
        if self.column is not None:
            parts.append(f"column {self.column}")
        return ": ".join((parts[0], ", ".join(parts[1:]))) if len(parts) > 1 else parts[0]


def _sort_key(defect: Defect) -> tuple[int, str, str]:
    """Deterministic order: file-level defects first, then by line, then by column name."""
    return (-1 if defect.line is None else defect.line, defect.column or "", defect.code.value)


def render_reason(defects: list[Defect]) -> str:
    """Render defects into the string stored on the batch.

    Truncation is explicit and counted rather than silent: a reason that quietly dropped defects
    would tell an operator they had fixed everything when they had not.
    """
    if not defects:
        raise ValueError("a quarantine reason requires at least one defect")

    ordered = sorted(defects, key=_sort_key)
    shown = ordered[:MAX_REPORTED_DEFECTS]
    reason = "; ".join(defect.render() for defect in shown)
    remaining = len(ordered) - len(shown)
    if remaining:
        reason = f"{reason}; (+{remaining} more)"

    # Belt and braces. Both are unreachable given the closed code set and the column names this
    # package uses today, and both are here because "unreachable" is a claim about code that has
    # not been written yet.
    #
    # The marker is words rather than an ellipsis. The first version used "..." and the allowlist
    # does not contain a full stop, so the truncation branch produced a string its own guard then
    # rejected — a quarantine would have raised instead of being recorded, leaving the batch stuck
    # at `received`. Found by a test that exercised the branch rather than reasoning about it.
    if len(reason) > MAX_REASON_LENGTH:
        reason = reason[: MAX_REASON_LENGTH - len(_TRUNCATED)] + _TRUNCATED
    if not REASON_ALLOWED.fullmatch(reason):
        raise ValueError("rendered quarantine reason contains characters outside the allowlist")
    return reason
