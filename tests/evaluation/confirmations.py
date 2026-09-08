"""Confirmed human labels for the hold-out slice, and the rule for applying them.

§20 asks for *"a human-labelled hold-out slice"*. `humanlabels.py` issues the packet and validates
what comes back; this module holds what came back, and decides what the golden set is allowed to do
with it.

**A confirmation that agrees is applied. A confirmation that disagrees is refused.**

That asymmetry is the whole design. Applying an agreeing label changes only the *provenance* of an
answer — the treatment is what it already was, and now a person has stood behind it. Applying a
disagreeing label would change the **answer key itself**, silently, on the authority of one
spreadsheet. The hold-out exists to catch a wrong label table; the correct response to it catching
one is an argument between a person and `labels.py`, resolved in a reviewed commit that changes the
rule — not a generator that quietly adopts whichever answer arrived last.

So :func:`apply_confirmations` raises on a disagreement and names every one. A disagreement is a
**finding**, which is what the packet's own instructions promise a labeller it will be.

**The file is committed, and the generator reads it.** A golden set that depended on a spreadsheet
in somebody's downloads folder would not be reproducible, and reproducibility is the property the
whole artefact rests on. The committed file is normalised text so a reviewer can read the diff.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
from collections.abc import Mapping, Sequence
from typing import Final

from ledger_exception_control_plane.db.control import TreatmentCode

__all__ = [
    "CONFIRMATIONS_PATH",
    "Confirmation",
    "ConfirmationConflict",
    "ConfirmationSet",
    "apply_confirmations",
    "load_confirmations",
    "render_confirmations",
]

_PACKET_DIR: Final = pathlib.Path(__file__).resolve().parents[1] / "golden" / "human-label-packet"
CONFIRMATIONS_PATH: Final = _PACKET_DIR / "confirmed.jsonl"


class ConfirmationConflict(Exception):  # noqa: N818 - a finding, not an error condition
    """A person's label differs from the derived one. Every conflict, never just the first.

    Deliberately fatal to the *generator* rather than handled by it. The golden set cannot be built
    while the two disagree, because there is no answer to build it from until somebody decides which
    is right — and making the generator choose would be making it decide.
    """

    def __init__(self, conflicts: Sequence[str]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "the confirmed labels disagree with the derived ones; this is a finding to resolve in "
            "`labels.py` or in the confirmation, not something the generator may decide: "
            + "; ".join(self.conflicts)
        )


@dataclasses.dataclass(frozen=True, slots=True)
class Confirmation:
    """One label a person confirmed, with who confirmed it and when."""

    exception_id: str
    treatment: TreatmentCode
    note: str
    confirmed_by: str
    confirmed_on: str


@dataclasses.dataclass(frozen=True, slots=True)
class ConfirmationSet:
    """Everything one returned packet confirmed, with the slice it was formed against."""

    hold_out_version: str
    hold_out_sha256: str
    source_file: str
    confirmations: Mapping[str, Confirmation]

    def __len__(self) -> int:
        return len(self.confirmations)


def render_confirmations(
    *,
    hold_out_version: str,
    hold_out_sha256: str,
    source_file: str,
    confirmations: Mapping[str, Confirmation],
) -> str:
    """The committed form: a header line, then one confirmation per line, sorted by id.

    The same shape as the golden set and the packet, for the same reason — provenance that can be
    separated from the artefact will be.
    """
    header = {
        "hold_out_version": hold_out_version,
        "hold_out_sha256": hold_out_sha256,
        "source_file": source_file,
        "confirmations": len(confirmations),
        "what_this_is": (
            "Human labels for the hold-out slice, confirmed against the slice whose digest is "
            "recorded above. Produced by a person, not by any program in this repository. A label "
            "here that disagrees with the derived one is refused by the generator rather than "
            "applied: see tests/evaluation/confirmations.py."
        ),
    }
    lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
    for exception_id in sorted(confirmations):
        entry = confirmations[exception_id]
        lines.append(
            json.dumps(
                {
                    "exception_id": entry.exception_id,
                    "treatment": entry.treatment.value,
                    "note": entry.note,
                    "confirmed_by": entry.confirmed_by,
                    "confirmed_on": entry.confirmed_on,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines) + "\n"


def load_confirmations(path: pathlib.Path | None = None) -> ConfirmationSet | None:
    """The committed confirmations, or ``None`` when none have been recorded.

    ``None`` rather than an empty set, so a caller has to decide what an unlabelled slice means
    instead of quietly treating it as one that agreed about nothing.
    """
    resolved = path or CONFIRMATIONS_PATH
    if not resolved.is_file():
        return None

    lines = [line for line in resolved.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None

    header = json.loads(lines[0])
    entries: dict[str, Confirmation] = {}
    for line in lines[1:]:
        row = json.loads(line)
        entries[row["exception_id"]] = Confirmation(
            exception_id=row["exception_id"],
            treatment=TreatmentCode(row["treatment"]),
            note=row.get("note", ""),
            confirmed_by=row["confirmed_by"],
            confirmed_on=row["confirmed_on"],
        )

    declared = int(header.get("confirmations", -1))
    if declared != len(entries):
        raise ValueError(
            f"{resolved.name} declares {declared} confirmation(s) and carries {len(entries)}"
        )
    return ConfirmationSet(
        hold_out_version=str(header["hold_out_version"]),
        hold_out_sha256=str(header["hold_out_sha256"]),
        source_file=str(header.get("source_file", "")),
        confirmations=entries,
    )


def apply_confirmations(
    exception_id: str,
    derived_treatment: str,
    confirmations: ConfirmationSet | None,
) -> Confirmation | None:
    """The confirmation for one record, if it agrees. Raises if it does not.

    Returns ``None`` when no person has confirmed this record — the ordinary case for the 225
    records outside the slice, and for the whole set before anybody labels it.
    """
    if confirmations is None:
        return None
    entry = confirmations.confirmations.get(exception_id)
    if entry is None:
        return None
    if entry.treatment.value != derived_treatment:
        raise ConfirmationConflict(
            [
                f"{exception_id}: derived {derived_treatment!r}, confirmed "
                f"{entry.treatment.value!r} by {entry.confirmed_by}"
            ]
        )
    return entry
