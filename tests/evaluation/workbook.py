"""The owner-facing workbook for §20's hold-out slice, and the reader that takes it back.

`humanlabels.py` owns the slice, the allowlist and the validator. This module owns one thing: how
that slice is presented to the person who labels it. Splitting them is the point — the workbook can
be made more legible without any edit reaching the thing that decides what a labeller may see.

**Every cell here is a function of the allowlist, and a test proves it.** The workbook adds
plain-language columns — a one-line evidence summary, what the decision is — because twenty-five
rows of bare field values ask a reviewer to do the reading a generator can do for them. But a
"summary" is exactly where an answer leaks in: one sentence mentioning what the label rule
concluded and the slice is worthless. So every derived column is built by a pure function of
:data:`~tests.evaluation.humanlabels.PERMITTED_FIELDS`, and
``test_every_derived_cell_is_recomputable_from_the_permitted_view`` recomputes each one and
asserts equality. A derived cell that consulted anything else — the golden record, the label, the
corpus — would differ, and the test would fail.

**The digest does not change and the version does not move.** ``hold_out_sha256`` is taken over the
permitted view, which is unchanged: the workbook restates the same facts, it does not add any. So a
label formed against the CSV and a label formed against the workbook are labels about the same
facts, and the validator accepts either.

Nothing here writes a label. There is no code path in this module that can put a value in
``HUMAN_LABEL``, and the workbook's own guard test asserts the column is empty in all 25 rows.
"""

from __future__ import annotations

import datetime as dt
import io
import pathlib
from collections.abc import Callable, Mapping
from typing import Any, Final

from ledger_exception_control_plane.db.control import TreatmentCode
from tests.evaluation.humanlabels import (
    ALLOWED_LABELS,
    BLANK_COLUMNS,
    LABEL_DEFINITIONS,
    PERMITTED_FIELDS,
    Packet,
    build_packet,
)

__all__ = [
    "ARTIFACT_DIR",
    "DERIVED_COLUMNS",
    "GUIDE_SHEET",
    "RECORDS_SHEET",
    "WORKBOOK_COLUMNS",
    "WORKBOOK_JSONL_PATH",
    "WORKBOOK_PATH",
    "counterpart_evidence",
    "decision_requested",
    "deterministic_evidence",
    "read_workbook_rows",
    "reference_evidence",
    "render_workbook",
]

ARTIFACT_DIR: Final = pathlib.Path(__file__).resolve().parents[2] / "artifacts"
WORKBOOK_PATH: Final = ARTIFACT_DIR / "human-label-packet.xlsx"
WORKBOOK_JSONL_PATH: Final = ARTIFACT_DIR / "human-label-packet.jsonl"

RECORDS_SHEET: Final = "HOLD_OUT"
GUIDE_SHEET: Final = "LABEL_GUIDE"


# ==========================================================================================
# The derived columns. Each is a pure function of the permitted view and of nothing else.
# ==========================================================================================


def _period_phrase(view: Mapping[str, str]) -> str:
    settled = view["settlement_period"] or "an unrecorded period"
    origin = view["originating_period"]
    if origin:
        return f"settled in {settled}, and the counterpart movement is recorded in {origin}"
    return f"settled in {settled}, with no counterpart period established"


def deterministic_evidence(view: Mapping[str, str]) -> str:
    """One sentence restating what the deterministic layers concluded about this line.

    A restatement and never a conclusion. It says what the matcher and classifier established; it
    does not say what follows from it, because what follows from it is the question.
    """
    kind = view["transaction_type"] or "an unclassified movement"
    return (
        f"The matcher could not clear this line against any ledger entry. "
        f"It is {kind} of {view['amount']} {view['currency']}, dated {view['value_date']}, "
        f"{_period_phrase(view)}. "
        f"The classifier reached `{view['classification']}`."
    )


def reference_evidence(view: Mapping[str, str]) -> str:
    """What the remittance/reference evidence supports, stated without concluding from it."""
    if view["has_merchant_reference"] == "yes":
        return (
            "A merchant reference travelled with the settlement line, so the counterparty and the "
            "original order can be identified from the remittance data."
        )
    return (
        "No merchant reference travelled with the settlement line. The counterparty cannot be "
        "identified from the remittance data alone."
    )


def counterpart_evidence(view: Mapping[str, str]) -> str:
    """What is known about a candidate ledger counterpart.

    **This is the honest version of "candidate ledger evidence".** The golden record carries no
    candidate ledger entry, because the deterministic matcher found none — that is why the line is
    an exception at all. What it does carry is whether a single originating period was established,
    which is the strongest counterpart signal available. Inventing a candidate column and filling
    it from nothing would be fabricating evidence.
    """
    origin = view["originating_period"]
    if origin:
        return (
            f"No ledger entry matched within tolerance. A single originating period ({origin}) was "
            "established for the movement this line appears to reverse."
        )
    return (
        "No ledger entry matched within tolerance, and no single originating period was "
        "established. An empty originating period means none was established — not that none "
        "exists."
    )


def decision_requested(view: Mapping[str, str]) -> str:
    """What the labeller is being asked. Identical on every row, and deliberately so.

    A per-row prompt would have to say something about the particular case, and anything it said
    would be a hint. The question is the same one twenty-five times: what is the correct accounting
    action here.
    """
    del view
    return (
        "Which single treatment is the correct accounting action for this exception, judged only "
        "from the evidence in this row? Choose it in HUMAN_LABEL. If the evidence does not support "
        "any of the other three, `escalate` is the correct answer and not a refusal to answer."
    )


#: The derived columns, in workbook order, each with the pure function that produces it.
DERIVED_COLUMNS: Final[Mapping[str, Callable[[Mapping[str, str]], str]]] = {
    "deterministic_evidence": deterministic_evidence,
    "reference_evidence": reference_evidence,
    "counterpart_evidence": counterpart_evidence,
    "decision_requested": decision_requested,
}

#: The workbook's columns, in order. The permitted facts, then the derived restatements of them,
#: then the vocabulary, then what the owner fills in, then the provenance the validator checks.
WORKBOOK_COLUMNS: Final = (
    "row",
    *PERMITTED_FIELDS,
    *DERIVED_COLUMNS,
    "allowed_labels",
    *BLANK_COLUMNS,
    "hold_out_version",
    "hold_out_sha256",
    "synthetic",
)


def workbook_rows(packet: Packet) -> list[dict[str, str]]:
    """Every row of the records sheet, as plain text. The single source both writers agree on."""
    rows: list[dict[str, str]] = []
    for index, view in enumerate(packet.records, start=1):
        row: dict[str, str] = {"row": str(index), **dict(view)}
        for name, derive in DERIVED_COLUMNS.items():
            row[name] = derive(view)
        row["allowed_labels"] = ALLOWED_LABELS
        for blank in BLANK_COLUMNS:
            row[blank] = ""
        row["hold_out_version"] = packet.hold_out_version
        row["hold_out_sha256"] = packet.digest
        row["synthetic"] = "no"
        rows.append(row)
    return rows


# ==========================================================================================
# The workbook
# ==========================================================================================

_GUIDE_INTRO: Final = (
    "This workbook holds {count} settlement exceptions from the committed golden set, for "
    "independent human labelling (PROJECT_SPEC.md section 20)."
)

_HOW_TO: Final = (
    (
        "Open the {sheet} sheet. Work one row at a time.",
        "",
    ),
    (
        "1. Read the evidence columns for that row and nothing else.",
        "There is no ordering to the rows and no relationship between them.",
    ),
    (
        "2. Choose exactly one treatment from the dropdown in HUMAN_LABEL.",
        "The cell only accepts the four values; anything else is rejected on import.",
    ),
    (
        "3. Put your reasoning in HUMAN_NOTE wherever the answer is arguable.",
        "HUMAN_NOTE never affects scoring. It is read by a person, not by the scorer.",
    ),
    (
        "4. Fill in labelled_by and labelled_on (ISO date, e.g. 2026-09-09) on every row.",
        "A human label with nobody attached is not recorded as a human label.",
    ),
    (
        "5. Leave every other column exactly as it is.",
        "hold_out_version and hold_out_sha256 are how the importer checks these labels were "
        "formed against these facts.",
    ),
    (
        "6. Save as .xlsx and return the file. Then it is imported with:",
        "uv run python -m tests.evaluation import-labels <your-file.xlsx>",
    ),
)

_WITHHELD: Final = (
    (
        "The expected treatment, the rule that produced it, and its reasoning.",
        "This slice exists to test that label table. Showing it would make the test circular.",
    ),
    (
        "Any model's proposal, and the recorded cassette's stand-in treatment.",
        "Those are the answers being graded, not evidence about the case.",
    ),
    (
        "The corpus's construction metadata, including its scenario identifier.",
        "It names what each case was built to be, which is the answer under another name.",
    ),
    (
        "The account policy — the table mapping a classification to a ledger account.",
        "This is the least obvious omission and the most important. It is an input to the derived "
        "labels this slice exists to test, so a labeller who had it would be re-running the "
        "derivation instead of checking it. Judge each case on its own facts.",
    ),
)


def render_workbook(packet: Packet) -> bytes:
    """The owner-facing workbook: one records sheet, one guide sheet, no labels.

    Returned as bytes rather than written, so a test can build and inspect one without touching
    the committed artefact.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = RECORDS_SHEET

    header_fill = PatternFill("solid", fgColor="1F3B54")
    fill_in_fill = PatternFill("solid", fgColor="4E7A1E")
    header_font = Font(bold=True, color="FFFFFF")

    sheet.append(list(WORKBOOK_COLUMNS))
    for index, name in enumerate(WORKBOOK_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=index)
        # The two columns the owner types into are coloured differently from the rest, because the
        # sheet is wide and "which cells are mine" should not need the guide sheet to answer.
        cell.fill = fill_in_fill if name in {"HUMAN_LABEL", "HUMAN_NOTE"} else header_fill
        cell.font = header_font
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for row in workbook_rows(packet):
        sheet.append([row[name] for name in WORKBOOK_COLUMNS])

    widths = {
        "row": 5,
        "exception_id": 38,
        "classification": 22,
        "transaction_type": 20,
        "amount": 12,
        "currency": 9,
        "value_date": 12,
        "settlement_period": 17,
        "originating_period": 18,
        "has_merchant_reference": 12,
        "deterministic_evidence": 78,
        "reference_evidence": 52,
        "counterpart_evidence": 60,
        "decision_requested": 60,
        "allowed_labels": 34,
        "HUMAN_LABEL": 16,
        "HUMAN_NOTE": 46,
        "labelled_by": 16,
        "labelled_on": 14,
        "hold_out_version": 9,
        "hold_out_sha256": 24,
        "synthetic": 10,
    }
    for index, name in enumerate(WORKBOOK_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = widths.get(name, 18)
    for index in range(2, len(packet.records) + 2):
        sheet.row_dimensions[index].height = 74
    for column in (
        "deterministic_evidence",
        "reference_evidence",
        "counterpart_evidence",
        "decision_requested",
        "HUMAN_NOTE",
    ):
        letter = get_column_letter(WORKBOOK_COLUMNS.index(column) + 1)
        for index in range(2, len(packet.records) + 2):
            sheet[f"{letter}{index}"].alignment = Alignment(vertical="top", wrap_text=True)

    sheet.freeze_panes = "B2"

    # A dropdown, so the common way to produce an invalid label — typing it — is not available.
    # The importer still validates: a dropdown is a convenience, never the enforcement.
    label_column = get_column_letter(WORKBOOK_COLUMNS.index("HUMAN_LABEL") + 1)
    validation = DataValidation(
        type="list",
        formula1=f'"{",".join(sorted(code.value for code in TreatmentCode))}"',
        allow_blank=True,
        showDropDown=False,
        errorTitle="Not a treatment code",
        error="Choose one of REBOOK, ACCRUE, WRITE_OFF or ESCALATE.",
        promptTitle="Treatment",
        prompt="Which accounting action is correct for this exception?",
    )
    sheet.add_data_validation(validation)
    validation.add(f"{label_column}2:{label_column}{len(packet.records) + 1}")

    _write_guide(book, packet)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _write_guide(book: Any, packet: Packet) -> None:
    """The LABEL_GUIDE sheet: the vocabulary, the instructions, and what is withheld."""
    from openpyxl.styles import Alignment, Font
    from openpyxl.utils import get_column_letter

    guide = book.create_sheet(GUIDE_SHEET)
    title = Font(bold=True, size=13)
    heading = Font(bold=True)
    wrap = Alignment(vertical="top", wrap_text=True)

    def say(left: str, right: str = "", *, font: Font | None = None) -> None:
        guide.append([left, right])
        row = guide.max_row
        if font is not None:
            guide.cell(row=row, column=1).font = font
        guide.cell(row=row, column=1).alignment = wrap
        guide.cell(row=row, column=2).alignment = wrap

    say(f"Hold-out label packet — version {packet.hold_out_version}", "", font=title)
    say(_GUIDE_INTRO.format(count=len(packet.records)))
    say("hold_out_sha256", packet.digest)
    say("")
    say("No label in this workbook was produced by any program.", "", font=heading)
    say(
        "Nothing in the generator can write a treatment. It writes an empty column and reads back "
        "a file somebody filled in."
    )
    say("")

    say("THE VOCABULARY — exactly one per row", "", font=heading)
    say(
        "Enter the token in the middle column exactly as written.",
        "The dropdown in HUMAN_LABEL offers those four and nothing else, so you cannot mistype "
        "one. The importer compares them exactly: it will not accept a different case, a hyphen "
        "or a trailing space, because guessing at a label is how a wrong one gets recorded.",
    )
    guide.append(["Treatment", "Enter exactly", "What it means as an accounting action"])
    for column in (1, 2, 3):
        guide.cell(row=guide.max_row, column=column).font = heading
        guide.cell(row=guide.max_row, column=column).alignment = wrap
    for code in TreatmentCode:
        guide.append([code.name, code.value, LABEL_DEFINITIONS[code.value]])
        for column in (1, 2, 3):
            guide.cell(row=guide.max_row, column=column).alignment = wrap
    say("")

    say("HOW TO LABEL", "", font=heading)
    for left, right in _HOW_TO:
        say(left.format(sheet=RECORDS_SHEET), right)
    say("")

    say("WHAT YOU ARE SHOWN", "", font=heading)
    say(
        f"Only the columns on the {RECORDS_SHEET} sheet.",
        "The evidence columns restate the fact columns in prose; they add nothing that is not "
        "already in them.",
    )
    say(
        "An empty originating_period",
        "means no single counterpart movement was established — not that none exists.",
    )
    say("")

    say("WHAT IS DELIBERATELY WITHHELD", "", font=heading)
    for left, right in _WITHHELD:
        say(left, right)
    say("")

    say("IF TWO LABELS WOULD POST THE SAME THING", "", font=heading)
    say(
        "Some rows can be read two ways that produce the same posting.",
        "Where the period a treatment would recognise the movement in is the same under two "
        "labels, the evidence cannot separate them. Pick the one whose *reason* fits, and say so "
        "in HUMAN_NOTE. That note is how a disagreement gets read as an ambiguity rather than as "
        "a wrong answer — no rule for breaking such a tie is given here, because a rule would be "
        "the answer for the rows it applies to.",
    )
    say("")

    say("DISAGREEMENT IS THE USEFUL OUTCOME", "", font=heading)
    say(
        "If your label differs from the one the generator derived, that is a finding to argue "
        "about, not an error in your row. A slice that agrees with the rules it audits audits "
        "nothing."
    )

    guide.column_dimensions[get_column_letter(1)].width = 46
    guide.column_dimensions[get_column_letter(2)].width = 22
    guide.column_dimensions[get_column_letter(3)].width = 92


def read_workbook_rows(path: pathlib.Path) -> list[dict[str, str]]:
    """Read a returned workbook's records sheet into the rows the validator expects.

    Reads the sheet by name, so a workbook with the guide sheet moved or an extra sheet added still
    imports. Values come back as text: the validator compares strings, and a spreadsheet that has
    helpfully turned a period into a date must not become a digest mismatch nobody can explain.
    """
    from openpyxl import load_workbook

    book = load_workbook(path, data_only=True, read_only=True)
    try:
        if RECORDS_SHEET not in book.sheetnames:
            raise KeyError(
                f"{path.name}: no {RECORDS_SHEET!r} sheet; this is not a hold-out label packet"
            )
        sheet = book[RECORDS_SHEET]
        rows = sheet.iter_rows(values_only=True)
        try:
            header = [str(cell) if cell is not None else "" for cell in next(rows)]
        except StopIteration:
            return []
        material: list[dict[str, str]] = []
        for values in rows:
            if all(value is None or str(value).strip() == "" for value in values):
                continue
            material.append(
                {name: _as_text(value) for name, value in zip(header, values, strict=False)}
            )
        return material
    finally:
        book.close()


def _as_text(value: object) -> str:
    """A cell as the text the validator will compare.

    Dates are the reason this exists. Excel silently retypes ``2026-06`` and an ISO date, and a
    round-tripped cell coming back as ``datetime`` would fail a digest comparison against the
    string it was written as — a rejection whose message would point at the wrong thing entirely.
    """
    if value is None:
        return ""
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return str(value).strip()


def write_artifacts(packet: Packet | None = None) -> tuple[pathlib.Path, pathlib.Path]:
    """Write both owner-facing artefacts and return their paths."""
    from tests.evaluation.humanlabels import render_packet_jsonl

    resolved = packet or build_packet()
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    WORKBOOK_PATH.write_bytes(render_workbook(resolved))
    WORKBOOK_JSONL_PATH.write_text(render_packet_jsonl(resolved), encoding="utf-8", newline="\n")
    return WORKBOOK_PATH, WORKBOOK_JSONL_PATH
