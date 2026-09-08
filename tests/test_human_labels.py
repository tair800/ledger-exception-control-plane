"""M6.3 groundwork — the human-label packet, and the ways a label set can be fabricated.

§20 requires *"a human-labelled hold-out slice"*, and ADR-060 recorded that nobody has labelled one
(OPEN-15). The risk this module exists to close is not that the slice stays unlabelled; it is that
it gets *filled in by something other than a person* and reported as human. Three failure modes,
and one test each:

1. **A leaky packet.** A labeller shown the expected treatment agrees with it, and the hold-out then
   confirms the label table it was supposed to audit. The negative tests below are the load-bearing
   ones.
2. **A fabricated import.** A file that covers half the slice, or repeats a record, or carries a
   label nobody recognises, or arrives with no attribution — each looks like a label set and is
   not one.
3. **A synthetic set presented as human.** Synthetic rows are needed to test the validator at all,
   so the format marks them and the code refuses to give them a human label source. The test for
   that is the one that matters most, because it is the fabrication `CLAUDE.md` §10 names.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
from collections.abc import Mapping
from typing import Any

import pytest

from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.money import DEMO_ACCOUNT_POLICY
from tests.evaluation import __main__ as cli
from tests.evaluation.golden import classification_of, load_golden_set
from tests.evaluation.humanlabels import (
    ALLOWED_LABELS,
    BLANK_COLUMNS,
    FORBIDDEN_IN_A_PACKET,
    HOLD_OUT_VERSION,
    LABEL_DEFINITIONS,
    PACKET_CSV,
    PACKET_JSONL,
    PACKET_README,
    PERMITTED_FIELDS,
    ImportRejected,
    Packet,
    build_packet,
    read_import,
    render_packet_csv,
    render_packet_jsonl,
    render_packet_readme,
    validate_import,
)
from tests.evaluation.labels import LabelSource

HOLD_OUT_RECORDS = 25


def _returned_rows(**overrides: Any) -> list[dict[str, str]]:
    """A complete, valid, **synthetic** return of the packet.

    Every label is ``escalate``, chosen because it is the least informative constant available: a
    fixture that happened to agree with the derived labels would make the tests below look like
    measurements. They are not measurements, and the ``synthetic`` marker says so in the data.
    """
    packet = build_packet()
    rows = []
    for row in packet.records:
        rows.append(
            {
                **row,
                "allowed_labels": ALLOWED_LABELS,
                "HUMAN_LABEL": TreatmentCode.ESCALATE.value,
                "HUMAN_NOTE": "",
                "labelled_by": "",
                "labelled_on": "",
                "hold_out_version": packet.hold_out_version,
                "hold_out_sha256": packet.digest,
                "synthetic": "yes",
                **overrides,
            }
        )
    return rows


def _as_human(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The same rows with the synthetic marker cleared and attribution filled in.

    Used **only** to exercise the non-synthetic branch of the validator. The labels are still not a
    person's judgement, which is exactly why no test below reports a figure computed from them.
    """
    return [
        {**row, "synthetic": "no", "labelled_by": "test-reviewer", "labelled_on": "2026-09-07"}
        for row in rows
    ]


# ======================================================================================
# The packet leaks nothing
# ======================================================================================


def test_the_packet_covers_the_frozen_hold_out_slice_exactly() -> None:
    golden = load_golden_set()
    packet = build_packet()

    assert len(packet.records) == HOLD_OUT_RECORDS
    assert packet.exception_ids == {record.exception_id for record in golden.hold_out}
    assert packet.hold_out_version == HOLD_OUT_VERSION
    assert len(packet.digest) == 64


def test_the_packet_contains_no_column_or_key_that_could_carry_the_answer() -> None:
    """**The structural leak check.** An allowlist of shown fields, asserted on the artefacts."""
    columns = next(iter(csv.reader(io.StringIO(PACKET_CSV.read_text(encoding="utf-8")))))
    leaked = set(columns) & FORBIDDEN_IN_A_PACKET
    assert not leaked, f"the packet CSV has a column that carries ground truth: {sorted(leaked)}"

    lines = PACKET_JSONL.read_text(encoding="utf-8").splitlines()
    for line in lines:
        keys = set(json.loads(line))
        leaked = keys & FORBIDDEN_IN_A_PACKET
        assert not leaked, f"the packet JSONL has a key that carries ground truth: {sorted(leaked)}"

    shown = set(columns) - set(BLANK_COLUMNS) - {"allowed_labels"}
    shown -= {"hold_out_version", "hold_out_sha256", "synthetic"}
    assert shown == set(PERMITTED_FIELDS), (
        "the CSV shows fields the allowlist does not name, or omits ones it does"
    )


def test_no_packet_file_contains_a_labels_reasoning_or_rule() -> None:
    """**The value-level leak check, and the stronger of the two.**

    A column check cannot catch an answer pasted into a free-text field, so this looks for the
    actual content: for every held-out record, its label rule and the sentence the label module
    wrote for it must appear nowhere in any packet file.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (PACKET_CSV, PACKET_JSONL, PACKET_README)
    )
    for record in load_golden_set().hold_out:
        assert record.label_why not in text, (
            f"{record.exception_id}: its reasoning is in the packet"
        )
        assert record.label_rule not in text, (
            f"{record.exception_id}: its label rule is in the packet"
        )


def test_no_row_carries_its_own_expected_treatment() -> None:
    """The one leak a vocabulary column makes easy to hide.

    ``allowed_labels`` lists all four treatments on every row and therefore says nothing about any
    particular one. What would say something is a *row-specific* field holding that row's answer, so
    every shown value is checked against the record's own expected treatment.
    """
    expected = {
        record.exception_id: record.expected_treatment for record in load_golden_set().hold_out
    }
    for row in csv.DictReader(io.StringIO(PACKET_CSV.read_text(encoding="utf-8"))):
        answer = expected[row["exception_id"]]
        for column, value in row.items():
            if column in {"allowed_labels", *BLANK_COLUMNS}:
                continue
            assert value != answer, f"{column} on {row['exception_id']} holds its own answer"


def test_the_account_policy_is_not_in_the_packet() -> None:
    """The withheld derivation, asserted rather than promised.

    For two of the four reachable classes the derived label follows from the account policy
    configuring nothing. A packet carrying that table would make the hold-out agree with the
    generator by construction, so no account code may appear anywhere in it.
    """
    text = "\n".join(
        path.read_text(encoding="utf-8") for path in (PACKET_CSV, PACKET_JSONL, PACKET_README)
    )
    accounts = {
        account
        for record in load_golden_set().records
        for treatment in TreatmentCode
        if (account := DEMO_ACCOUNT_POLICY.account_for(classification_of(record), treatment))
        is not None
    }

    assert accounts, "no account is configured at all; this test would then be vacuous"
    for account in accounts:
        assert account not in text, f"the packet discloses account {account}"


def test_every_blank_column_is_actually_blank() -> None:
    """The module writes the question, never the answer."""
    for row in csv.DictReader(io.StringIO(PACKET_CSV.read_text(encoding="utf-8"))):
        for column in BLANK_COLUMNS:
            assert row[column] == "", f"{column} is pre-filled with {row[column]!r}"

    for line in PACKET_JSONL.read_text(encoding="utf-8").splitlines()[1:]:
        record = json.loads(line)
        for column in BLANK_COLUMNS:
            assert record[column] is None


def test_the_packet_states_the_vocabulary_and_defines_every_member() -> None:
    """A labeller cannot answer in a closed vocabulary they were not given."""
    header = json.loads(PACKET_JSONL.read_text(encoding="utf-8").splitlines()[0])
    assert header["allowed_labels"] == sorted(code.value for code in TreatmentCode)
    assert set(header["label_definitions"]) == {code.value for code in TreatmentCode}

    readme = PACKET_README.read_text(encoding="utf-8")
    for code in TreatmentCode:
        assert f"`{code.value}`" in readme
        assert LABEL_DEFINITIONS[code.value] in readme

    assert "withheld" in readme and "account policy" in readme, (
        "the instructions must say what is deliberately withheld, or a labeller will ask for it"
    )


def test_the_committed_packet_matches_its_generator() -> None:
    """The same drift check the corpus, the cassette, the golden set and the baseline get."""
    packet = build_packet()
    assert PACKET_CSV.read_bytes() == render_packet_csv(packet).encode("utf-8")
    assert PACKET_JSONL.read_bytes() == render_packet_jsonl(packet).encode("utf-8")
    assert PACKET_README.read_bytes() == render_packet_readme(packet).encode("utf-8")
    for path in (PACKET_CSV, PACKET_JSONL, PACKET_README):
        assert b"\r" not in path.read_bytes(), f"{path.name} is not LF-only"


def test_the_digest_covers_what_the_labeller_was_shown() -> None:
    """A change to a shown fact must invalidate every label formed against it.

    Not a change to a field nobody saw: the digest is over the shown view precisely so a valid
    import is not refused for something that could not have affected the judgement.
    """
    packet = build_packet()
    mutated = Packet(
        hold_out_version=packet.hold_out_version,
        records=({**packet.records[0], "amount": "0.01"}, *packet.records[1:]),
    )
    assert mutated.digest != packet.digest

    renamed = Packet(hold_out_version="2", records=packet.records)
    assert renamed.digest != packet.digest, "the version must be inside the digest"


# ======================================================================================
# The import validator
# ======================================================================================


def test_a_complete_return_validates() -> None:
    packet = build_packet()
    imported = validate_import(_as_human(_returned_rows()), packet)

    assert len(imported.labels) == HOLD_OUT_RECORDS
    assert imported.hold_out_version == HOLD_OUT_VERSION
    assert imported.hold_out_digest == packet.digest
    assert imported.synthetic is False
    assert imported.is_evaluation_evidence is True
    assert imported.label_source is LabelSource.HUMAN


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        ("a missing record", lambda rows: rows[:-1]),
        ("a duplicated record", lambda rows: [*rows, dict(rows[0])]),
        (
            "an unknown label",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "re-book"}, *rows[1:]],
        ),
        (
            "a label differing only in case",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "ESCALATE"}, *rows[1:]],
        ),
        (
            "a label with trailing punctuation",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "escalate?"}, *rows[1:]],
        ),
        ("a blank label", lambda rows: [{**rows[0], "HUMAN_LABEL": ""}, *rows[1:]]),
        (
            "a label for a record not in the slice",
            lambda rows: [
                *rows[1:],
                {**rows[0], "exception_id": "00000000-0000-0000-0000-000000000000"},
            ],
        ),
        (
            "a stale hold-out version",
            lambda rows: [{**row, "hold_out_version": "0"} for row in rows],
        ),
        (
            "a stale digest",
            lambda rows: [{**row, "hold_out_sha256": "0" * 64} for row in rows],
        ),
        (
            "two different digests in one file",
            lambda rows: [{**rows[0], "hold_out_sha256": "0" * 64}, *rows[1:]],
        ),
        (
            "half the file marked synthetic",
            lambda rows: [{**rows[0], "synthetic": "yes"}, *rows[1:]],
        ),
        (
            "no attribution on a non-synthetic row",
            lambda rows: [{**rows[0], "labelled_by": "", "labelled_on": ""}, *rows[1:]],
        ),
        ("an empty file", lambda rows: []),
    ],
)
def test_the_validator_refuses_a_bad_import(what: str, mutate: Any) -> None:
    """**Thirteen ways a label set can look complete and not be one.**

    Each is refused rather than repaired. A validator that dropped a duplicate, lower-cased a
    label or accepted a subset would turn a spreadsheet somebody rushed into evidence — and the
    whole reason §20 wants a human slice is that it is the one part of the harness a wrong label
    table cannot fake.
    """
    with pytest.raises(ImportRejected):
        validate_import(mutate(_as_human(_returned_rows())), build_packet())


def test_a_refusal_lists_every_reason_rather_than_the_first() -> None:
    """A reviewer fixing a returned file needs the whole list, not twenty-five runs of the tool."""
    rows = _as_human(_returned_rows())
    rows[0]["HUMAN_LABEL"] = ""
    rows[1]["HUMAN_LABEL"] = "nonsense"
    rows[2]["labelled_by"] = ""

    with pytest.raises(ImportRejected) as raised:
        validate_import(rows, build_packet())

    assert len(raised.value.reasons) >= 3
    assert any("no HUMAN_LABEL" in reason for reason in raised.value.reasons)
    assert any("nonsense" in reason for reason in raised.value.reasons)
    assert any("labelled_by" in reason for reason in raised.value.reasons)


# ======================================================================================
# A synthetic import can never be presented as human
# ======================================================================================


def test_a_synthetic_import_is_refused_a_human_label_source() -> None:
    """**The test that matters most in this module.**

    Synthetic rows exist so the validator's mechanics can be exercised. They must never become
    evidence, and the refusal is structural: asking a synthetic import for its label source raises
    rather than returning a value a caller could write into the golden set.
    """
    imported = validate_import(_returned_rows(), build_packet())

    assert imported.synthetic is True
    assert imported.is_evaluation_evidence is False
    with pytest.raises(ValueError, match="marked synthetic"):
        _ = imported.label_source


def test_the_cli_exits_non_zero_for_a_synthetic_import(tmp_path: pathlib.Path) -> None:
    """A synthetic file cannot be accepted by the tool either, whatever a caller ignores.

    Non-zero, so a script that piped this into anything stops. The message says what the labels
    are and what must not be done with them.
    """
    path = tmp_path / "synthetic-labels.csv"
    rows = _returned_rows()
    _write_csv(path, rows)

    assert cli.main(["import-labels", str(path)]) == 1


def test_the_cli_accepts_a_complete_attributed_return(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "labels.csv"
    _write_csv(path, _as_human(_returned_rows()))
    assert cli.main(["import-labels", str(path)]) == 0


def test_the_cli_refuses_an_incomplete_return(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "partial.csv"
    _write_csv(path, _as_human(_returned_rows())[:10])
    assert cli.main(["import-labels", str(path)]) == 1


def test_a_returned_jsonl_is_accepted_too(tmp_path: pathlib.Path) -> None:
    """The companion format, so a reviewer working in a script is not forced into a spreadsheet."""
    packet = build_packet()
    path = tmp_path / "labels.jsonl"
    header = {
        "hold_out_version": packet.hold_out_version,
        "hold_out_sha256": packet.digest,
        "synthetic": "no",
    }
    lines = [json.dumps(header)]
    lines.extend(json.dumps(row) for row in _as_human(_returned_rows()))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    imported = read_import(path, packet)
    assert len(imported.labels) == HOLD_OUT_RECORDS
    assert imported.label_source is LabelSource.HUMAN


def test_an_unrecognised_file_type_is_refused(tmp_path: pathlib.Path) -> None:
    """A file the importer has no reader for is refused by extension, before it is opened."""
    path = tmp_path / "labels.txt"
    path.write_bytes(b"not a csv")
    with pytest.raises(ImportRejected, match=r"expected a \.xlsx, \.csv or \.jsonl"):
        read_import(path, build_packet())


def test_a_file_that_is_not_really_a_workbook_is_refused_rather_than_crashing(
    tmp_path: pathlib.Path,
) -> None:
    """**A wrong attachment is a mistake, not a stack trace.**

    Naming a file `.xlsx` routes it to the workbook reader, and a reader handed nine bytes raises
    `BadZipFile` — which tells the owner nothing about what they did. It surfaces as the same kind
    of refusal every other bad import gets.
    """
    path = tmp_path / "labels.xlsx"
    path.write_bytes(b"not a csv")
    with pytest.raises(ImportRejected, match="not a readable label workbook"):
        read_import(path, build_packet())


def test_the_committed_golden_set_still_claims_no_human_label() -> None:
    """The gap this module exists to make closable is still open, and says so.

    OPEN-15 is discharged by a person confirming the slice, not by shipping the mechanism to do it.
    Asserting the state here keeps the tooling and the claim honest about each other.
    """
    golden = load_golden_set()
    assert golden.human_labelled == ()
    assert all(record.label_source == LabelSource.DERIVED.value for record in golden.records)


def _write_csv(path: pathlib.Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def test_the_helpers_here_produce_no_label_the_packet_could_absorb() -> None:
    """A guard on this test module itself.

    ``_returned_rows`` produces labels, and it must stay impossible to mistake them for anything: a
    constant ``escalate``, marked synthetic by default, and reachable only through
    :func:`_as_human` for the one branch that needs a non-synthetic import. If a future fixture
    started deriving plausible labels here, that would be a label generator living in the test
    suite — the exact thing the packet's design forbids.
    """
    rows: list[Mapping[str, str]] = list(_returned_rows())
    assert {row["HUMAN_LABEL"] for row in rows} == {TreatmentCode.ESCALATE.value}
    assert {row["synthetic"] for row in rows} == {"yes"}
