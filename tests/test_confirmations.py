"""Applying a confirmed hold-out label — and refusing to apply a disagreeing one.

The hold-out came back agreeing with the derived table on all 25 records, so the interesting branch
of this module never fires in the committed artefact. That is exactly why it is tested here: a
refusal that has never run is a refusal nobody has seen work, and the one time it matters will be
the time somebody's judgement contradicts `labels.py`.

No database, no network, no model.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ledger_exception_control_plane.db.control import TreatmentCode
from tests.evaluation.confirmations import (
    CONFIRMATIONS_PATH,
    ConfirmationConflict,
    apply_confirmations,
    load_confirmations,
    render_confirmations,
)
from tests.evaluation.golden import load_golden_set
from tests.evaluation.humanlabels import HOLD_OUT_VERSION, build_packet

# ======================================================================================
# The committed confirmations
# ======================================================================================


def test_the_committed_confirmations_cover_the_frozen_slice_exactly() -> None:
    """25 confirmations, for the 25 records that were sent, formed against this digest."""
    confirmed = load_confirmations()
    assert confirmed is not None

    packet = build_packet()
    assert len(confirmed) == 25
    assert set(confirmed.confirmations) == packet.exception_ids
    assert confirmed.hold_out_sha256 == packet.digest
    assert confirmed.hold_out_version == HOLD_OUT_VERSION


def test_every_confirmation_names_a_person_and_a_date() -> None:
    """A human label with nobody attached is the gap it was meant to close, under a new name."""
    confirmed = load_confirmations()
    assert confirmed is not None
    for entry in confirmed.confirmations.values():
        assert entry.confirmed_by.strip(), f"{entry.exception_id} has no confirmer"
        assert entry.confirmed_on.strip(), f"{entry.exception_id} has no date"
        assert entry.treatment in set(TreatmentCode)


def test_the_committed_confirmations_agree_with_the_derived_labels() -> None:
    """**The result of the hold-out, asserted as a fact about the committed files.**

    Recomputed here rather than quoted from a report: the agreement figure only means something if
    it is derived from the two artefacts every time the suite runs. If a future change to
    `labels.py` moved a derived label away from what the owner confirmed, this fails — which is the
    hold-out doing its job, a year later, without anybody re-running it by hand.
    """
    confirmed = load_confirmations()
    assert confirmed is not None
    derived = {
        record.exception_id: record.expected_treatment
        for record in load_golden_set().records
        if record.held_out
    }

    disagreements = {
        eid: (derived[eid], entry.treatment.value)
        for eid, entry in confirmed.confirmations.items()
        if derived[eid] != entry.treatment.value
    }
    assert disagreements == {}, (
        "the confirmed labels no longer agree with the derived ones. This is a finding about "
        f"`labels.py`, not a test to update: {disagreements}"
    )
    assert len(confirmed) == len(derived) == 25


def test_the_confirmation_file_is_reproducible_from_its_own_contents() -> None:
    """The committed file is what the renderer produces, so a hand edit shows up as drift."""
    confirmed = load_confirmations()
    assert confirmed is not None
    rendered = render_confirmations(
        hold_out_version=confirmed.hold_out_version,
        hold_out_sha256=confirmed.hold_out_sha256,
        source_file=confirmed.source_file,
        confirmations=confirmed.confirmations,
    )
    assert rendered == CONFIRMATIONS_PATH.read_text(encoding="utf-8")


def test_the_confirmation_file_carries_no_derived_answer() -> None:
    """It holds what a person said, not what the generator said.

    A confirmation file that also carried the derived treatment would let a future reader — or a
    future bug — compare the two by reading one file, and the first thing anybody would do with
    that is write a loop that "reconciles" them.
    """
    text = CONFIRMATIONS_PATH.read_text(encoding="utf-8")
    for line in text.splitlines()[1:]:
        row = json.loads(line)
        assert set(row) == {
            "exception_id",
            "treatment",
            "note",
            "confirmed_by",
            "confirmed_on",
        }, row
    for forbidden in ("expected_treatment", "label_rule", "label_why", "escalation_is_correct"):
        assert forbidden not in text


# ======================================================================================
# The rule: agreement is applied, disagreement is refused
# ======================================================================================


def test_an_agreeing_confirmation_is_applied() -> None:
    applied = apply_confirmations(
        "031f0372-0b57-5e6d-959d-5da0d9c4c53b",
        TreatmentCode.REBOOK.value,
        load_confirmations(),
    )
    assert applied is not None
    assert applied.treatment is TreatmentCode.REBOOK
    assert applied.confirmed_by


def test_a_disagreeing_confirmation_is_refused_and_names_both_answers() -> None:
    """**The branch the real hold-out never exercised, and the reason the module exists.**

    A person's label that differs from the derived one must not be applied — not because the person
    is presumed wrong, but because adopting it silently would let one spreadsheet rewrite the answer
    key that everything else in §20 is graded against. The generator refuses and the disagreement
    is argued about.

    The refusal names both treatments and the confirmer, because "they disagree" is not actionable
    and "derived accrue, confirmed rebook, by X" is.
    """
    confirmed = load_confirmations()
    assert confirmed is not None
    eid = next(iter(confirmed.confirmations))

    with pytest.raises(ConfirmationConflict) as conflict:
        apply_confirmations(eid, TreatmentCode.WRITE_OFF.value, confirmed)

    message = str(conflict.value)
    assert eid in message
    assert "write_off" in message
    assert confirmed.confirmations[eid].treatment.value in message
    assert confirmed.confirmations[eid].confirmed_by in message
    assert "not something the generator may decide" in message


def test_a_record_nobody_confirmed_is_left_alone() -> None:
    """225 records were never sent to anybody, and must stay derived."""
    assert (
        apply_confirmations("not-a-real-id", TreatmentCode.ESCALATE.value, load_confirmations())
        is None
    )


def test_no_confirmations_at_all_is_distinguishable_from_confirmations_of_nothing() -> None:
    """``None`` rather than an empty set, so a caller cannot treat "unlabelled" as "agreed"."""
    assert load_confirmations(pathlib.Path("does-not-exist.jsonl")) is None
    assert apply_confirmations("anything", TreatmentCode.ESCALATE.value, None) is None


def test_a_confirmation_file_that_miscounts_itself_is_refused(tmp_path: pathlib.Path) -> None:
    """A truncated file must not silently confirm fewer records than it claims."""
    confirmed = load_confirmations()
    assert confirmed is not None
    lines = CONFIRMATIONS_PATH.read_text(encoding="utf-8").splitlines()
    truncated = tmp_path / "confirmed.jsonl"
    truncated.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="declares 25"):
        load_confirmations(truncated)


def test_nothing_in_this_repository_writes_a_confirmation_from_a_derived_label() -> None:
    """**The guard that keeps the whole exercise honest.**

    A confirmation is a claim that a person said something. The only writer of the committed file
    is the one-off import path, which reads a returned workbook — and `humanlabels.py` refuses a
    synthetic import a human label source. What must never appear is code that builds a
    ``Confirmation`` out of a `GoldenRecord`'s own answer, because that would manufacture the
    agreement this slice exists to test for.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    suspicious: list[str] = []
    for path in (root / "tests").rglob("*.py"):
        if path.name in {"test_confirmations.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        if "Confirmation(" not in text:
            continue
        for marker in ("expected_treatment", "label.treatment", "derived_treatment="):
            if marker in text:
                suspicious.append(f"{path.relative_to(root).as_posix()} builds one near {marker}")
    assert suspicious == [], suspicious
