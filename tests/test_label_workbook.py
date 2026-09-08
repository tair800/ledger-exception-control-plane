"""The owner-facing hold-out workbook: what it must contain, and what it must never contain.

`tests/test_human_labels.py` already holds the slice's own guarantees — the allowlist, the digest,
and thirteen ways an import can be wrong. This module covers the thing that was added on top of
them: a spreadsheet with prose in it.

**Prose is where an answer leaks.** A summary column is written to help a reviewer read the case,
and one sentence saying what the label rule concluded would make the whole hold-out worthless. So
the central test here is not about spreadsheets at all: it recomputes every derived cell from the
permitted view and asserts equality, which is only possible if the derivation consulted nothing
else.

No database, no network, no model.
"""

from __future__ import annotations

import datetime as dt
import io
import pathlib
from typing import Any, Final

import pytest
from openpyxl import Workbook, load_workbook

from ledger_exception_control_plane.db.control import TreatmentCode
from tests.evaluation.golden import load_golden_set
from tests.evaluation.humanlabels import (
    BLANK_COLUMNS,
    FORBIDDEN_IN_A_PACKET,
    LABEL_DEFINITIONS,
    PERMITTED_FIELDS,
    ImportRejected,
    build_packet,
    read_import,
)
from tests.evaluation.workbook import (
    DERIVED_COLUMNS,
    GUIDE_SHEET,
    RECORDS_SHEET,
    WORKBOOK_COLUMNS,
    WORKBOOK_JSONL_PATH,
    WORKBOOK_PATH,
    render_workbook,
    workbook_rows,
)

pytestmark = pytest.mark.filterwarnings("ignore::UserWarning")


@pytest.fixture(scope="module")
def packet() -> Any:
    return build_packet()


@pytest.fixture(scope="module")
def book(packet: Any) -> Any:
    """The workbook as generated, read back from bytes. Never the committed file."""
    return load_workbook(io.BytesIO(render_workbook(packet)))


@pytest.fixture(scope="module")
def sheet(book: Any) -> Any:
    return book[RECORDS_SHEET]


def _header(sheet: Any) -> list[str]:
    return [str(cell.value) for cell in sheet[1]]


def _data_rows(sheet: Any) -> list[dict[str, Any]]:
    names = _header(sheet)
    rows = []
    for values in sheet.iter_rows(min_row=2, values_only=True):
        if all(value is None or str(value).strip() == "" for value in values):
            continue
        rows.append(dict(zip(names, values, strict=True)))
    return rows


# ======================================================================================
# What the owner receives
# ======================================================================================


def test_the_workbook_holds_exactly_the_twenty_five_held_out_records(
    sheet: Any, packet: Any
) -> None:
    """**Exactly the frozen slice — not a sample of it, and not a row more.**

    Asserted against the golden set's own held-out flag rather than the literal 25, so the test
    still means something if the slice is ever legitimately resized: what it pins is that the
    workbook and the slice are the same set, and separately that the slice is the documented size.
    """
    rows = _data_rows(sheet)
    held = [record for record in load_golden_set().records if record.held_out]

    assert len(rows) == len(held) == 25
    assert {row["exception_id"] for row in rows} == {record.exception_id for record in held}
    assert len({row["exception_id"] for row in rows}) == 25, "a record appears twice"
    assert [row["row"] for row in rows] == [str(n) for n in range(1, 26)]


def test_every_cell_the_owner_fills_in_is_empty(sheet: Any) -> None:
    """The four columns a person writes, blank in all 25 rows.

    Including `labelled_by` and `labelled_on`: a pre-filled name is an attribution nobody made.
    """
    for row in _data_rows(sheet):
        for column in BLANK_COLUMNS:
            assert row[column] in (None, ""), f"{column} is pre-filled for {row['exception_id']}"


def test_the_workbook_offers_exactly_the_authoritative_vocabulary(sheet: Any, book: Any) -> None:
    """The four codes, from the enum, everywhere the owner can see them.

    **The accepted token is the enum's value and it is lower case.** The names are `REBOOK`,
    `ACCRUE`, `WRITE_OFF` and `ESCALATE`; the strings `TreatmentCode(...)` accepts are `rebook`,
    `accrue`, `write_off` and `escalate`, and the validator refuses anything that is not one of
    those exactly — deliberately, since guessing at a label is how a wrong one gets recorded.

    So the dropdown offers the tokens, which is what makes the strictness safe: the owner picks
    from a list and cannot produce a spelling the importer will reject. The guide carries both, so
    a reader who knows the codes by name can still find the one they mean.
    """
    tokens = {code.value for code in TreatmentCode}
    names = {code.name for code in TreatmentCode}
    assert tokens == {"rebook", "accrue", "write_off", "escalate"}
    assert names == {"REBOOK", "ACCRUE", "WRITE_OFF", "ESCALATE"}

    for row in _data_rows(sheet):
        assert set(str(row["allowed_labels"]).split("|")) == tokens

    validations = list(sheet.data_validations.dataValidation)
    assert len(validations) == 1, "the label column needs exactly one dropdown"
    offered = validations[0].formula1.strip('"').split(",")
    assert set(offered) == tokens, "the dropdown must offer exactly the strings the importer takes"

    guide = "\n".join(
        str(cell.value) for row in book[GUIDE_SHEET].iter_rows() for cell in row if cell.value
    )
    for code in TreatmentCode:
        assert code.value in guide, f"the guide never gives the token for {code.name}"
        assert code.name in guide, f"the guide never names {code.name}"
        assert LABEL_DEFINITIONS[code.value] in guide, f"the guide never defines {code.name}"


def test_the_guide_sheet_exists_and_explains_the_task(book: Any) -> None:
    """A vocabulary with no instructions is a dropdown, not a guide."""
    assert book.sheetnames == [RECORDS_SHEET, GUIDE_SHEET]
    guide = "\n".join(
        str(cell.value) for row in book[GUIDE_SHEET].iter_rows() for cell in row if cell.value
    )
    for phrase in (
        "HUMAN_LABEL",
        "HUMAN_NOTE",
        "labelled_by",
        "hold_out_sha256",
        "DELIBERATELY WITHHELD",
        "account policy",
        "escalate",
    ):
        assert phrase in guide, f"the guide never mentions {phrase}"


# ======================================================================================
# Leakage
# ======================================================================================


def test_every_derived_cell_is_recomputable_from_the_permitted_view(
    sheet: Any, packet: Any
) -> None:
    """**The load-bearing test in this module.**

    Every prose column is rebuilt here from the permitted view alone — the same nine fields the
    labeller can already see — and compared to what the workbook holds. A derivation that reached
    for the golden record, the label, the label rule or the corpus would produce a different string
    and this would fail.

    It is what makes a free-text column safe: not a scan for suspicious words, which a paraphrase
    defeats, but a proof that the text is a function of facts the labeller already has.
    """
    views = {row["exception_id"]: row for row in packet.records}
    assert set(views) == {row["exception_id"] for row in _data_rows(sheet)}

    for row in _data_rows(sheet):
        view = views[row["exception_id"]]
        for column, derive in DERIVED_COLUMNS.items():
            assert row[column] == derive(view), f"{column} for {row['exception_id']} is not derived"


def test_the_workbook_has_no_column_that_could_carry_the_answer(sheet: Any) -> None:
    """The forbidden names, as columns. The allowlist is the mechanism; this is the assertion."""
    header = {name.lower() for name in _header(sheet)}
    assert header & {name.lower() for name in FORBIDDEN_IN_A_PACKET} == set()
    fact_columns = {name for name in _header(sheet) if name in PERMITTED_FIELDS}
    assert fact_columns == set(PERMITTED_FIELDS), "the fact columns must be exactly the allowlist"


def test_no_cell_anywhere_carries_a_labels_reasoning_rule_or_expected_treatment(book: Any) -> None:
    """A value-level sweep of **every cell of every sheet**, against the real answer key.

    Stronger than the column check and the reason it is separate: a leak that mattered would not
    announce itself with a forbidden column name. It would be the label's own reasoning pasted into
    a summary. So this takes what each held-out record actually concluded and asserts none of it
    appears anywhere in the file.
    """
    text = "\n".join(
        str(cell.value)
        for name in book.sheetnames
        for row in book[name].iter_rows()
        for cell in row
        if cell.value is not None
    )
    lowered = text.lower()

    for record in load_golden_set().records:
        if not record.held_out:
            continue
        assert record.label_why.lower() not in lowered, f"{record.exception_id} leaks its reasoning"
        assert record.label_rule.lower() not in lowered, f"{record.exception_id} leaks its rule"

    # The scenario identifier is the answer under another name.
    for record in load_golden_set().records:
        scenario = getattr(record, "scenario_id", None)
        assert scenario is None, "GoldenRecord must not carry scenario_id at all"


def test_no_row_states_its_own_expected_treatment(sheet: Any) -> None:
    """A row may print the four-code vocabulary; it may not single out its own answer.

    `allowed_labels` and the guide's prose legitimately contain every code, so the check is
    per-row and excludes the columns whose whole job is to list the vocabulary: outside those, a
    row must not name the treatment that happens to be its expected one.
    """
    expected = {
        record.exception_id: record.expected_treatment
        for record in load_golden_set().records
        if record.held_out
    }
    vocabulary_columns = {"allowed_labels", "decision_requested"}

    for row in _data_rows(sheet):
        answer = expected[row["exception_id"]].upper()
        for column, value in row.items():
            if column in vocabulary_columns or value is None:
                continue
            assert answer not in str(value).upper(), (
                f"{row['exception_id']}: column {column} names its own expected treatment"
            )


def test_the_account_policy_is_absent_from_the_workbook(book: Any) -> None:
    """The withheld table, named as withheld and never printed.

    The guide is *allowed* to say the account policy is withheld — that sentence is the point. What
    must not appear is the policy's content: an account code mapped to a classification.
    """
    from ledger_exception_control_plane.money import policy

    accounts = {
        name: getattr(policy, name)
        for name in dir(policy)
        if name.startswith("ACCOUNT_") and isinstance(getattr(policy, name), str)
    }
    assert accounts, "no account codes found; this guard would pass over nothing"

    text = "\n".join(
        str(cell.value)
        for name in book.sheetnames
        for row in book[name].iter_rows()
        for cell in row
        if cell.value is not None
    )
    for name, code in accounts.items():
        assert code not in text, (
            f"the workbook prints {name} ({code}), which is the withheld policy"
        )


def test_the_derived_prose_never_states_a_conclusion(sheet: Any) -> None:
    """A summary may restate the facts; it may not tell the labeller what follows from them.

    The phrases below are the ways a helpful sentence turns into an answer. Checked because the
    recomputability test proves only that the text is a *function* of the permitted view — a
    function could still map those facts to "so this should be REBOOK", and that would be derived
    and still fatal.
    """
    banned = (
        "should be",
        "should therefore",
        "the correct treatment is",
        "the answer is",
        "this is a rebook",
        "this is an accrue",
        "this is a write_off",
        "recommend",
        "suggests that the treatment",
    )
    for row in _data_rows(sheet):
        for column in DERIVED_COLUMNS:
            value = str(row[column]).lower()
            for phrase in banned:
                assert phrase not in value, f"{column} for {row['exception_id']} says {phrase!r}"


def test_the_shipped_package_still_does_not_import_a_spreadsheet_library() -> None:
    """openpyxl is a dev dependency, and the application must stay unaware of it.

    A packet generator is evaluation tooling. If `src/` ever imports it, the dependency is a runtime
    one and this repository has started shipping a spreadsheet writer inside a control plane.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "openpyxl" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"the shipped package imports openpyxl: {offenders}"


# ======================================================================================
# Importing a returned workbook
# ======================================================================================


SYNTHETIC_LABEL: Final = TreatmentCode.ESCALATE.value


def _returned_workbook(
    packet: Any,
    *,
    mutate: Any = None,
    synthetic: bool = True,
) -> bytes:
    """A filled-in workbook, built for testing mechanics only.

    **Always synthetic unless a test explicitly says otherwise**, and the labels are a constant
    rather than anything resembling a judgement. These rows exist to drive the validator; the
    format's `synthetic` marker is what stops them ever being counted, and
    `test_human_labels.py::test_a_synthetic_import_is_refused_a_human_label_source` is what proves
    the marker works.
    """
    rows = workbook_rows(packet)
    for row in rows:
        row["HUMAN_LABEL"] = SYNTHETIC_LABEL
        row["HUMAN_NOTE"] = "synthetic row, produced by a test to exercise the validator"
        row["labelled_by"] = "automated-test"
        row["labelled_on"] = dt.date(2026, 9, 9).isoformat()
        row["synthetic"] = "yes" if synthetic else "no"
    if mutate is not None:
        rows = mutate(rows)

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = RECORDS_SHEET
    sheet.append(list(WORKBOOK_COLUMNS))
    for row in rows:
        sheet.append([row.get(name, "") for name in WORKBOOK_COLUMNS])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _write(tmp_path: pathlib.Path, payload: bytes, name: str = "returned.xlsx") -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(payload)
    return path


def test_a_complete_synthetic_workbook_imports(tmp_path: pathlib.Path, packet: Any) -> None:
    """The happy path, and it is still refused a human source — which is the guarantee."""
    imported = read_import(_write(tmp_path, _returned_workbook(packet)), packet)

    assert len(imported.labels) == 25
    assert imported.synthetic is True
    assert imported.is_evaluation_evidence is False
    with pytest.raises(ValueError, match="synthetic"):
        _ = imported.label_source


def test_a_workbook_round_trips_the_digest_unchanged(tmp_path: pathlib.Path, packet: Any) -> None:
    """The provenance survives Excel. A retyped period would fail here rather than mysteriously."""
    imported = read_import(_write(tmp_path, _returned_workbook(packet)), packet)
    assert imported.hold_out_digest == packet.digest
    assert imported.hold_out_version == packet.hold_out_version


@pytest.mark.parametrize(
    ("what", "mutate"),
    [
        (
            "an invalid treatment code",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "re-book"}, *rows[1:]],
        ),
        (
            "an upper-cased treatment code",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "REBOOK"}, *rows[1:]],
        ),
        (
            "a treatment code with an inner space",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "write off"}, *rows[1:]],
        ),
        (
            "a hyphenated treatment code",
            lambda rows: [{**rows[0], "HUMAN_LABEL": "write-off"}, *rows[1:]],
        ),
        ("a duplicated record id", lambda rows: [rows[0], *rows]),
        ("a missing label", lambda rows: [{**rows[0], "HUMAN_LABEL": ""}, *rows[1:]]),
        (
            "an unexpected record id",
            lambda rows: [
                {**rows[0], "exception_id": "00000000-0000-0000-0000-000000000000"},
                *rows[1:],
            ],
        ),
        ("a dropped record", lambda rows: rows[1:]),
        (
            "an altered hold-out fingerprint",
            lambda rows: [{**row, "hold_out_sha256": "0" * 64} for row in rows],
        ),
        (
            "a stale packet version",
            lambda rows: [{**row, "hold_out_version": "0"} for row in rows],
        ),
    ],
)
def test_the_importer_refuses_a_bad_workbook(
    tmp_path: pathlib.Path, packet: Any, what: str, mutate: Any
) -> None:
    """Eight ways a returned workbook can look finished and not be usable.

    Each is refused, never repaired: the importer does not lower-case a label, drop a duplicate,
    infer a blank or migrate a digest. A validator that did any of those would turn a spreadsheet
    somebody rushed into evidence.
    """
    with pytest.raises(ImportRejected):
        read_import(_write(tmp_path, _returned_workbook(packet, mutate=mutate)), packet)


def test_surrounding_whitespace_is_tolerated_and_nothing_else_is(
    tmp_path: pathlib.Path, packet: Any
) -> None:
    """The one normalisation the importer performs, pinned so its edge is deliberate.

    A cell that comes back as `"escalate "` is a spreadsheet artefact, not a different answer, and
    refusing it would send the owner hunting for an invisible character. So the label is stripped
    and then matched **exactly** — which is a different thing from guessing: `"write-off"`,
    `"write off"` and `"WRITE_OFF"` all remain refusals, because each is a decision about what
    somebody meant.
    """
    padded = _returned_workbook(
        packet,
        mutate=lambda rows: [{**row, "HUMAN_LABEL": f"  {row['HUMAN_LABEL']}  "} for row in rows],
    )
    imported = read_import(_write(tmp_path, padded, "padded.xlsx"), packet)

    assert len(imported.labels) == 25
    assert {label.treatment for label in imported.labels.values()} == {TreatmentCode.ESCALATE}


def test_the_importer_never_fills_in_a_missing_label(tmp_path: pathlib.Path, packet: Any) -> None:
    """A blank is an unanswered question, and the refusal names the row rather than guessing.

    `ESCALATE` is a real answer a person has to choose. An importer that treated an empty cell as
    "escalate" would manufacture the most common label in the set and inflate agreement with the
    derived table — precisely the failure the hold-out exists to detect.
    """
    blanked = _returned_workbook(
        packet, mutate=lambda rows: [{**rows[0], "HUMAN_LABEL": ""}, *rows[1:]]
    )
    with pytest.raises(ImportRejected) as refusal:
        read_import(_write(tmp_path, blanked), packet)

    reasons = refusal.value.reasons
    assert any("no HUMAN_LABEL" in reason for reason in reasons)

    # One reason, naming the row. Not two: the record *was* present in the file, so reporting it
    # as absent from the slice as well would send the owner looking for a row that is already there.
    assert not any("absent" in reason for reason in reasons), reasons

    # And nothing in the refusal suggests what the label should have been.
    assert not any(TreatmentCode.ESCALATE.value in reason for reason in reasons), reasons


def test_a_note_does_not_change_what_is_imported(tmp_path: pathlib.Path, packet: Any) -> None:
    """HUMAN_NOTE is carried and never scored. Two files differing only in notes agree exactly."""
    plain = read_import(_write(tmp_path, _returned_workbook(packet), "a.xlsx"), packet)
    noted = read_import(
        _write(
            tmp_path,
            _returned_workbook(
                packet,
                mutate=lambda rows: [
                    {**row, "HUMAN_NOTE": f"note {i}"} for i, row in enumerate(rows)
                ],
            ),
            "b.xlsx",
        ),
        packet,
    )

    assert {k: v.treatment for k, v in plain.labels.items()} == {
        k: v.treatment for k, v in noted.labels.items()
    }
    assert noted.labels[next(iter(noted.labels))].note.startswith("note ")


def test_a_workbook_without_the_records_sheet_is_refused(tmp_path: pathlib.Path) -> None:
    """A different spreadsheet is not a hold-out packet, and the refusal names the missing sheet."""
    book = Workbook()
    assert book.active is not None
    book.active.title = "Sheet1"
    buffer = io.BytesIO()
    book.save(buffer)

    with pytest.raises(ImportRejected, match=RECORDS_SHEET):
        read_import(_write(tmp_path, buffer.getvalue()))


def test_the_committed_artifacts_match_their_generator(packet: Any) -> None:
    """`make label-packet` is the only way these files change, and drift is caught here.

    The workbook is compared on content rather than bytes: a zip container carries timestamps, so
    two runs produce different files holding identical sheets, and asserting on bytes would fail
    for a reason that has nothing to do with the packet.
    """
    from tests.evaluation.humanlabels import render_packet_jsonl

    assert WORKBOOK_PATH.is_file(), "artifacts/human-label-packet.xlsx has not been generated"
    assert WORKBOOK_JSONL_PATH.is_file(), (
        "artifacts/human-label-packet.jsonl has not been generated"
    )
    assert WORKBOOK_JSONL_PATH.read_text(encoding="utf-8") == render_packet_jsonl(packet)

    committed = load_workbook(WORKBOOK_PATH)
    fresh = load_workbook(io.BytesIO(render_workbook(packet)))
    assert committed.sheetnames == fresh.sheetnames
    for name in fresh.sheetnames:
        assert [[cell.value for cell in row] for row in committed[name].iter_rows()] == [
            [cell.value for cell in row] for row in fresh[name].iter_rows()
        ], f"{name} has drifted"


def test_nothing_in_this_module_can_produce_a_label_the_packet_would_absorb() -> None:
    """The test helper writes one constant and marks every row synthetic.

    Asserted rather than assumed, because this file is the only place in the repository that writes
    a value into a `HUMAN_LABEL` cell. If it ever produced a *varied* label set it would look like
    a judgement, and the distance between that and evidence is one flag somebody flips.
    """
    source = pathlib.Path(__file__).read_text(encoding="utf-8")

    # Every assignment into a HUMAN_LABEL cell anywhere in this file, found by reading the source
    # rather than by trusting the helper above. There must be exactly one, it must write the single
    # constant, and the constant must be one fixed treatment.
    assignments = [
        line.strip()
        for line in source.splitlines()
        if '["HUMAN_LABEL"]' in line and "=" in line and "assert" not in line
    ]
    assert assignments == ['row["HUMAN_LABEL"] = SYNTHETIC_LABEL'], assignments
    assert isinstance(SYNTHETIC_LABEL, str)
    assert TreatmentCode(SYNTHETIC_LABEL) is TreatmentCode.ESCALATE

    # And every synthetic workbook declares itself synthetic.
    assert 'row["synthetic"] = "yes" if synthetic else "no"' in source
