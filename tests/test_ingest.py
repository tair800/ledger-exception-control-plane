"""Ingestion tests — deterministic, Docker-free.

Two things are asserted here that no amount of reading would establish: that money survives the
text-to-value boundary exactly, and that this package has not quietly become the matcher M2.2
owns. Both are checked structurally as well as behaviourally, because both decay silently.
"""

from __future__ import annotations

import ast
import csv
import datetime as dt
import decimal
import hashlib
import io
import json
import pathlib

import pytest
from sqlalchemy import String

import ledger_exception_control_plane.ingest as ingest_package
from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
    Base,
)
from ledger_exception_control_plane.fixtures.serialise import SETTLEMENT_COLUMNS as FIXTURE_COLUMNS
from ledger_exception_control_plane.ingest.errors import (
    MAX_REASON_LENGTH,
    MAX_REPORTED_DEFECTS,
    REASON_ALLOWED,
    Defect,
    QuarantineCode,
    render_reason,
)
from ledger_exception_control_plane.ingest.normalise import (
    MAX_MERCHANT_REFERENCE,
    MAX_PSP_REFERENCE,
)
from ledger_exception_control_plane.ingest.parser import SETTLEMENT_COLUMNS, parse
from ledger_exception_control_plane.ingest.service import content_hash, interpret

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "fixtures" / "canonical"
INGEST_ROOT = pathlib.Path(ingest_package.__file__).resolve().parent

HEADER = ",".join(SETTLEMENT_COLUMNS)


def row(**overrides: str) -> str:
    """A valid settlement row, with fields replaced by name."""
    fields = {
        "psp_reference": "psp_000000000001",
        "merchant_reference": "ORD-2026-100001",
        "transaction_type": "capture",
        "amount": "120.45",
        "currency": "EUR",
        "value_date": "2026-06-03",
        "presentment_amount": "",
        "presentment_currency": "",
        "fx_rate": "",
        "memo": "capture",
    }
    fields.update(overrides)
    # A real CSV writer, so a field containing a comma is quoted rather than silently splitting
    # the row. Joining with commas by hand made an amount of "1,45" arrive as a field-count
    # mismatch, which is a true diagnosis of the bytes but not the case the test meant to make.
    buffer = io.StringIO(newline="")
    csv.writer(buffer, lineterminator="").writerow([fields[c] for c in SETTLEMENT_COLUMNS])
    return buffer.getvalue()


def payload(*rows: str, header: str = HEADER) -> bytes:
    return ("\n".join((header, *rows)) + "\n").encode("utf-8")


def only_defect(raw: bytes) -> Defect:
    lines, defects = interpret(raw)
    assert not lines, "a batch with defects must yield no lines"
    assert len(defects) == 1, f"expected exactly one defect, got {[d.code for d in defects]}"
    return defects[0]


# --------------------------------------------------------------------------------------
# The declared format
# --------------------------------------------------------------------------------------


def test_the_parser_and_the_fixture_generator_agree_on_the_format() -> None:
    """Both implement ADR-031, and neither imports the format from the other.

    Production reading its column list from the test corpus would be production depending on its
    own test data. They are declared separately and reconciled here, which catches drift in either
    direction without creating that dependency.
    """
    assert SETTLEMENT_COLUMNS == FIXTURE_COLUMNS


def test_the_declared_field_limits_match_the_columns_they_will_be_written_to() -> None:
    """Validation limits are declared in the normaliser; the schema is the authority."""

    def limit(column: str) -> int:
        column_type = Base.metadata.tables["settlement_line"].columns[column].type
        assert isinstance(column_type, String)
        assert column_type.length is not None
        return column_type.length

    assert limit("psp_reference") == MAX_PSP_REFERENCE
    assert limit("merchant_reference") == MAX_MERCHANT_REFERENCE


# --------------------------------------------------------------------------------------
# The canonical corpus round-trips
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["2026-06", "2026-07"])
def test_the_canonical_settlement_files_normalise_to_the_recorded_values(period: str) -> None:
    """The strongest test available: parse what the generator wrote and compare with what it
    recorded it had written.

    ``records.json`` is construction ground truth for the *values* — the generator built those
    lines and rendered the CSV from them. It carries no classification, no match intent and no
    expected outcome, and this test reads none. It is the field-value half of the corpus, which is
    exactly what a normaliser should be judged against.
    """
    corpus = json.loads((CORPUS / "records.json").read_text(encoding="utf-8"))
    path = f"settlement/psp-settlement-{period}.csv"
    batch = next(b for b in corpus["batches"] if b["raw_payload_path"] == path)

    lines, defects = interpret((CORPUS / path).read_bytes())
    assert not defects, f"the canonical corpus must ingest cleanly: {[d.code for d in defects]}"
    assert len(lines) == len(batch["lines"])

    for produced, recorded in zip(lines, batch["lines"], strict=True):
        assert produced.line_number == recorded["line_number"]
        assert produced.psp_reference == recorded["psp_reference"]
        assert produced.merchant_reference == recorded["merchant_reference"]
        assert produced.currency == recorded["currency"]
        assert produced.value_date == dt.date.fromisoformat(recorded["value_date"])
        # String comparison, not numeric: it proves the *scale* survived too, so a JPY amount
        # does not come back with invented decimal places.
        assert str(produced.amount) == recorded["amount"]
        assert isinstance(produced.amount, decimal.Decimal)


def test_the_corpus_repeated_psp_reference_is_accepted_not_rejected() -> None:
    """SC-012 repeats one reference on two rows deliberately.

    The PSP's reference is their data, not a key this system can rely on, and the schema is unique
    on ``(batch, line_number)`` rather than on it. A normaliser that rejected the repetition would
    quarantine a valid file — and would be asserting a matching rule it does not own.
    """
    lines, defects = interpret((CORPUS / "settlement/psp-settlement-2026-06.csv").read_bytes())
    assert not defects
    references = [line.psp_reference for line in lines]
    assert len(references) != len(set(references)), "the canonical corpus should contain a repeat"


def test_normalisation_is_deterministic_across_repeated_runs() -> None:
    raw = (CORPUS / "settlement/psp-settlement-2026-06.csv").read_bytes()
    first, _ = interpret(raw)
    second, _ = interpret(raw)
    assert first == second


# --------------------------------------------------------------------------------------
# Money
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["0", "1", "-1", "0.00", "120.45", "-120.45", "1.2345", "-1.2345", "9999999999999999.9999"],
)
def test_a_valid_amount_becomes_the_exact_decimal_the_text_stated(text: str) -> None:
    """Exact, and with the scale the file wrote.

    ``str(amount) == text`` is stronger than numeric equality: it proves nothing quantised the
    value, nothing normalised its representation, and no float was involved. ``float("0.1")`` is
    not 0.1, and a system that had gone through one could not satisfy this.
    """
    lines, defects = interpret(payload(row(amount=text)))
    assert not defects, [d.code for d in defects]
    assert str(lines[0].amount) == text
    assert isinstance(lines[0].amount, decimal.Decimal)


def test_a_high_precision_amount_survives_a_value_float_could_not_hold() -> None:
    """A value with more significant digits than a float has bits."""
    text = "9999999999999.9999"
    lines, _ = interpret(payload(row(amount=text)))
    assert str(lines[0].amount) == text
    assert lines[0].amount != decimal.Decimal(float(text)), (
        "if this passes through float the value changes, which is the whole point"
    )


@pytest.mark.parametrize("text", ["1.23456", "0.00005", "-1.23456", "0.12345"])
def test_an_over_precise_amount_is_rejected_and_never_rounded(text: str) -> None:
    """ADR-020's rule, applied at the ingestion boundary rather than only at the column.

    Rejected exactly when four decimal places cannot hold the value — never rounded to fit.
    """
    assert only_defect(payload(row(amount=text))).code is QuarantineCode.AMOUNT_PRECISION_EXCEEDED


@pytest.mark.parametrize("text", ["1.230000", "120.450000", "1.000000", "0.0000000", "1.00000"])
def test_trailing_zeros_beyond_four_places_are_accepted_not_quarantined(text: str) -> None:
    """The rule is value-based, and ADR-020 chose that deliberately.

    ``120.450000`` loses nothing at four decimal places, and the column stores it — ADR-020's own
    verification table lists ``1.230000`` as accepted. A representation-based check would refuse an
    entire settlement batch because a PSP's renderer emits a fixed six-decimal scale, which is a
    false quarantine rather than a guarantee. This was the first implementation, and an adversarial
    review caught that it contradicted the ADR it cited.

    The value is stored as written: nothing quantises it on the way in.

    Compared as ``as_tuple()`` rather than ``str()`` because ``Decimal`` renders a zero with more
    than six decimal places in scientific notation — ``str(Decimal("0.0000000"))`` is ``"0E-7"``.
    That is a rendering property, not a loss: the coefficient and exponent are exactly what the
    file stated, which is what the assertion needs to establish.
    """
    lines, defects = interpret(payload(row(amount=text)))
    assert not defects, [d.code for d in defects]
    assert lines[0].amount.as_tuple() == decimal.Decimal(text).as_tuple()


def test_the_ingestion_precision_rule_agrees_with_the_column_constraint() -> None:
    """Accept-here / reject-at-the-column is the failure mode this pins shut.

    The column's rule is ``trunc(amount, 4) = amount``. Anything the normaliser accepts must
    satisfy it, or a quarantine becomes an unhandled integrity error at INSERT time.
    """
    for text in ["1.230000", "120.450000", "1.0000", "0", "-1.2345", "9999999999999999.9999"]:
        lines, defects = interpret(payload(row(amount=text)))
        assert not defects, f"{text} was refused by ingestion"
        value = lines[0].amount
        truncated = (
            value.scaleb(MONEY_MAX_SCALE)
            .to_integral_value(rounding=decimal.ROUND_DOWN)
            .scaleb(-MONEY_MAX_SCALE)
        )
        assert value == truncated, f"{text} would violate trunc(amount, 4) = amount"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not-a-number",
        "1,45",
        "1.2.3",
        "1e3",
        "1E+3",
        "NaN",
        "Infinity",
        "-Infinity",
        "+1.00",
        " 1.00",
        "1.00 ",
        "٣.١٤",
    ],
)
def test_an_unreadable_amount_is_rejected(text: str) -> None:
    """``NaN`` and ``1E+3`` construct perfectly well as ``Decimal``. The regex is what stops them.

    ``1,45`` is a comma decimal separator and ``٣.١٤`` is Arabic-Indic digits — both would be
    accepted by a locale-aware parse, and both are rejected here because ingestion must not depend
    on the machine it runs on.
    """
    assert only_defect(payload(row(amount=text))).code is QuarantineCode.INVALID_AMOUNT


def test_an_amount_beyond_the_schema_magnitude_is_rejected() -> None:
    too_large = str(MONEY_MAGNITUDE_EXCLUSIVE_BOUND)
    assert only_defect(payload(row(amount=too_large))).code is QuarantineCode.AMOUNT_OUT_OF_RANGE


def test_the_maximum_permitted_scale_is_accepted() -> None:
    """The complement: a rule that rejected everything would pass every test above."""
    text = "1." + "0" * MONEY_MAX_SCALE
    lines, defects = interpret(payload(row(amount=text)))
    assert not defects
    assert str(lines[0].amount) == text


def test_signed_amounts_are_preserved() -> None:
    """Refunds, chargebacks and fees are legitimately negative (the schema imposes no sign rule)."""
    lines, _ = interpret(payload(row(amount="-326.92", transaction_type="chargeback")))
    assert lines[0].amount == decimal.Decimal("-326.92")


# --------------------------------------------------------------------------------------
# Currency, dates, references
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["eur", "EURO", "EU", "", "E1R", "Eur", "  "])
def test_an_invalid_currency_is_rejected(code: str) -> None:
    defect = only_defect(payload(row(currency=code)))
    assert defect.code is QuarantineCode.INVALID_CURRENCY
    assert defect.column == "currency"


@pytest.mark.parametrize("code", ["EUR", "USD", "JPY", "BHD", "XXX"])
def test_a_well_formed_currency_is_preserved_exactly(code: str) -> None:
    """Validated for shape, never translated, never converted."""
    lines, defects = interpret(payload(row(currency=code)))
    assert not defects
    assert lines[0].currency == code


@pytest.mark.parametrize(
    "text",
    ["", "03/06/2026", "2026-6-3", "20260603", "2026-13-01", "2026-02-30", "not-a-date"],
)
def test_an_invalid_date_is_rejected(text: str) -> None:
    """``03/06/2026`` is 3 June or 6 March depending on where you are. Neither is accepted."""
    assert only_defect(payload(row(value_date=text))).code is QuarantineCode.INVALID_DATE


def test_a_value_date_stays_a_date() -> None:
    """The source states a day. Widening it to a timestamp would invent precision (ADR-023)."""
    lines, _ = interpret(payload(row(value_date="2026-06-03")))
    assert lines[0].value_date == dt.date(2026, 6, 3)
    assert not isinstance(lines[0].value_date, dt.datetime)


def test_a_missing_psp_reference_is_rejected_and_a_missing_merchant_reference_is_not() -> None:
    """One is required by the schema, the other is nullable because the PSP may not pass it."""
    assert only_defect(payload(row(psp_reference=""))).code is QuarantineCode.MISSING_REQUIRED_FIELD
    assert only_defect(payload(row(psp_reference="   "))).code is (
        QuarantineCode.MISSING_REQUIRED_FIELD
    )

    lines, defects = interpret(payload(row(merchant_reference="")))
    assert not defects
    assert lines[0].merchant_reference is None


def test_references_are_preserved_exactly_and_not_canonicalised() -> None:
    """No case folding, no punctuation stripping, no whitespace collapsing.

    How close two references must be before they denote one movement is a *matching* rule, and
    M2.2 owns it. Deciding it here would bake it into the persisted record where no later test
    could vary it — and would destroy differences a matcher may need to see (ADR-039).
    """
    awkward = "  PSP_Ref-001  with inner  spaces  "
    lines, defects = interpret(payload(row(psp_reference=awkward)))
    assert not defects
    assert lines[0].psp_reference == awkward


def test_an_over_long_reference_is_rejected_rather_than_truncated() -> None:
    assert only_defect(payload(row(psp_reference="p" * 129))).code is QuarantineCode.FIELD_TOO_LONG
    assert (
        only_defect(payload(row(merchant_reference="m" * 129))).code
        is QuarantineCode.FIELD_TOO_LONG
    )


# --------------------------------------------------------------------------------------
# Cross-currency columns — validated, never converted
# --------------------------------------------------------------------------------------


def test_a_coherent_cross_currency_row_is_accepted_and_the_rate_is_carried_not_applied() -> None:
    lines, defects = interpret(
        payload(
            row(
                amount="4499.93",
                currency="EUR",
                presentment_amount="683880",
                presentment_currency="JPY",
                fx_rate="0.00658",
            )
        )
    )
    assert not defects
    line = lines[0]
    assert line.presentment_amount == decimal.Decimal("683880")
    assert line.presentment_currency == "JPY"
    assert line.fx_rate == decimal.Decimal("0.00658")
    # The settlement amount is what the file said, not the product of anything.
    assert line.amount == decimal.Decimal("4499.93")


@pytest.mark.parametrize(
    ("overrides", "column"),
    [
        ({"presentment_amount": "100"}, "presentment_amount"),
        ({"presentment_currency": "JPY"}, "presentment_amount"),
        ({"fx_rate": "0.0065"}, "fx_rate"),
    ],
)
def test_incoherent_presentment_columns_are_rejected(
    overrides: dict[str, str], column: str
) -> None:
    """A presented amount with no currency is a number with no unit."""
    defect = only_defect(payload(row(**overrides)))
    assert defect.code is QuarantineCode.INCOHERENT_PRESENTMENT_FIELDS
    assert defect.column == column


@pytest.mark.parametrize("rate", ["0", "-0.5", "abc", "1e-3"])
def test_an_invalid_fx_rate_is_rejected(rate: str) -> None:
    defect = only_defect(
        payload(row(presentment_amount="100", presentment_currency="JPY", fx_rate=rate))
    )
    assert defect.code is QuarantineCode.INVALID_FX_RATE


def test_an_fx_rate_may_carry_more_precision_than_money() -> None:
    """A rate is not money and does not borrow money's four-decimal ceiling (ADR-031)."""
    lines, defects = interpret(
        payload(row(presentment_amount="100", presentment_currency="JPY", fx_rate="0.00658123"))
    )
    assert not defects
    assert lines[0].fx_rate == decimal.Decimal("0.00658123")


# --------------------------------------------------------------------------------------
# Structural defects
# --------------------------------------------------------------------------------------


def test_an_empty_payload_is_rejected() -> None:
    assert only_defect(b"").code is QuarantineCode.EMPTY_PAYLOAD


def test_a_payload_that_is_not_utf8_is_rejected_without_echoing_it() -> None:
    defect = only_defect(HEADER.encode("utf-8") + b"\n\xff\xfe\x00invalid\n")
    assert defect.code is QuarantineCode.UNDECODABLE_PAYLOAD


@pytest.mark.parametrize(
    "header",
    [
        ",".join(SETTLEMENT_COLUMNS[:-1]),  # a column missing
        ",".join((*SETTLEMENT_COLUMNS, "extra")),  # an unexpected column
        ",".join(reversed(SETTLEMENT_COLUMNS)),  # reordered
        ",".join(c.upper() for c in SETTLEMENT_COLUMNS),  # renamed
    ],
)
def test_a_header_that_is_not_the_declared_format_is_rejected(header: str) -> None:
    assert only_defect(payload(row(), header=header)).code is QuarantineCode.HEADER_MISMATCH


def test_a_short_row_is_rejected() -> None:
    assert only_defect(payload("psp_1,ORD-1,capture")).code is (
        QuarantineCode.ROW_FIELD_COUNT_MISMATCH
    )


def test_a_header_only_file_is_valid_and_yields_no_lines() -> None:
    """A settlement period with no movements is a real thing; quarantining it would be wrong.

    Distinct from an empty payload, which carries no header and is a truncated or failed transfer.
    """
    lines, defects = interpret((HEADER + "\n").encode("utf-8"))
    assert not defects
    assert lines == ()


def test_a_utf8_bom_does_not_masquerade_as_a_header_mismatch() -> None:
    lines, defects = interpret(b"\xef\xbb\xbf" + payload(row()))
    assert not defects
    assert len(lines) == 1


def test_a_quoted_field_containing_a_comma_is_read_as_one_field() -> None:
    lines, defects = interpret(payload(row(memo="as discussed, partial")))
    assert not defects
    assert lines[0].memo == "as discussed, partial"


# --------------------------------------------------------------------------------------
# Batch-level quarantine
# --------------------------------------------------------------------------------------


def test_one_bad_row_discards_the_whole_batch() -> None:
    """FR-2 quarantines *batches*. Accepting the rows that happened to parse would manufacture a
    trusted partial settlement file, and every movement missing from it would later look like an
    unexplained residual (ADR-040)."""
    lines, defects = interpret(
        payload(
            row(psp_reference="psp_a"),
            row(psp_reference="psp_b", amount="bad"),
            row(psp_reference="psp_c"),
        )
    )
    assert lines == (), "no line from a rejected batch may survive"
    assert len(defects) == 1
    assert defects[0].line == 2


def test_every_defect_in_a_row_is_reported_not_just_the_first() -> None:
    """So a file is fixed in one pass rather than one defect per delivery."""
    _, defects = interpret(payload(row(amount="bad", currency="eur", value_date="nope")))
    assert {d.code for d in defects} == {
        QuarantineCode.INVALID_AMOUNT,
        QuarantineCode.INVALID_CURRENCY,
        QuarantineCode.INVALID_DATE,
    }


# --------------------------------------------------------------------------------------
# The quarantine reason is safe to store
# --------------------------------------------------------------------------------------


def test_a_reason_is_bounded_deterministic_and_free_of_input() -> None:
    defects = [Defect(line, "amount", QuarantineCode.INVALID_AMOUNT) for line in range(1, 40)]
    reason = render_reason(defects)
    assert len(reason) <= MAX_REASON_LENGTH
    assert REASON_ALLOWED.fullmatch(reason)
    assert reason == render_reason(list(reversed(defects))), "order must not depend on input order"
    assert reason.count(";") == MAX_REPORTED_DEFECTS, "truncation must be counted, not silent"
    assert "+34 more" in reason


def test_a_reason_never_carries_the_offending_value_or_an_exception_message() -> None:
    """The payload is already retained in full; repeating a fragment of it in a control record
    buys nothing and puts file content somewhere it was not meant to travel (§17)."""
    secret_looking = "psp_AKIAIOSFODNN7EXAMPLE"
    _, defects = interpret(payload(row(psp_reference=secret_looking, amount="not-a-number")))
    reason = render_reason(list(defects))
    assert secret_looking not in reason
    assert "not-a-number" not in reason
    for marker in ("Traceback", 'File "', "line 1, in", "Error(", "asyncpg", "postgresql://"):
        assert marker not in reason


def test_a_reason_requires_at_least_one_defect() -> None:
    with pytest.raises(ValueError, match="at least one defect"):
        render_reason([])


def test_every_quarantine_code_renders_within_the_allowlist() -> None:
    """A future code carrying a stray character must fail here, not in a control record."""
    for code in QuarantineCode:
        assert REASON_ALLOWED.fullmatch(render_reason([Defect(1, "amount", code)]))


# --------------------------------------------------------------------------------------
# The receipt
# --------------------------------------------------------------------------------------


def test_the_content_hash_is_taken_from_the_original_bytes() -> None:
    """Before decoding, before the BOM is stripped, before anything is normalised.

    Hashing a cleaned-up form would let two different files share a hash, and the re-delivery
    guard would then suppress a genuinely new batch.
    """
    raw = b"\xef\xbb\xbf" + payload(row())
    assert content_hash(raw) == hashlib.sha256(raw).hexdigest()
    assert content_hash(raw) != content_hash(payload(row())), (
        "a BOM makes it a different artifact, and the hash must say so"
    )


# --------------------------------------------------------------------------------------
# Scope: this package must not become M2.2 or M2.3
# --------------------------------------------------------------------------------------


def _ingest_sources() -> list[tuple[str, ast.Module]]:
    paths = sorted(INGEST_ROOT.rglob("*.py"))
    assert len(paths) >= 5, "the guards must be walking real files"
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in paths]


def test_no_float_appears_anywhere_in_the_ingestion_package() -> None:
    """The plan's exit criterion for M2.1, enforced structurally.

    ``float`` has no legitimate use here — not for money, not for a rate, not for a comparison —
    so the guard is absence of the name rather than an attempt to trace which call sites are
    monetary.
    """
    for name, tree in _ingest_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "float", f"{name} references float"
            if isinstance(node, ast.Attribute):
                assert node.attr != "float", f"{name} references float"


def test_the_ingestion_package_imports_nothing_that_could_make_it_a_matcher() -> None:
    """An allowlist, so a reconciliation module added later fails this rather than slipping past.

    ``db.control`` is excluded deliberately: it holds the exception, proposal and approval tables,
    and ingestion has no business touching any of them.
    """
    permitted_stdlib = {
        "__future__",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "hashlib",
        "io",
        "re",
        "typing",
        "uuid",
    }
    permitted_third_party = {"sqlalchemy"}
    permitted_internal = {
        "ledger_exception_control_plane.ingest",
        "ledger_exception_control_plane.db.base",
        "ledger_exception_control_plane.db.models",
    }
    for name, tree in _ingest_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                if module.startswith("ledger_exception_control_plane"):
                    assert any(module.startswith(p) for p in permitted_internal), (
                        f"{name} imports {module}, which is outside the ingestion boundary"
                    )
                else:
                    assert module.split(".")[0] in permitted_stdlib | permitted_third_party, (
                        f"{name} imports {module}, which is not on the ingestion allowlist"
                    )


def test_the_ingestion_package_never_references_reconciliation_concepts() -> None:
    """Names M2.2 and M2.3 own. A ``match_state`` written here would ship an answer with the row."""
    forbidden = {
        "LedgerEntry",
        "MatchResult",
        "MatchState",
        "ExceptionClassification",
        "ExceptionRecord",
        "TreatmentProposal",
        "match_state",
        "ledger_entry",
        "match_result",
        "tolerance",
    }
    for name, tree in _ingest_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id not in forbidden, f"{name} references {node.id}"
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden, f"{name} references {node.attr}"
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in forbidden, f"{name} contains the literal {node.value!r}"


def test_the_ingestion_package_defines_no_function_that_names_a_later_increment() -> None:
    verbs = ("match", "classify", "reconcile", "tolerance", "propose", "compute_adjustment")
    for name, tree in _ingest_sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                lowered = node.name.lstrip("_").lower()
                assert not lowered.startswith(verbs), (
                    f"{name} defines {node.name}, which names behaviour a later increment owns"
                )


def test_production_ingestion_does_not_depend_on_the_fixture_package() -> None:
    """The corpus is test input. Production reading it — or its ground-truth labels — would make
    every later test that uses the corpus circular."""
    for name, tree in _ingest_sources():
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            for module in modules:
                assert "fixtures" not in module, f"{name} imports {module}"
        source = ast.dump(tree)
        for label in ("intended_classification", "scenario_id", "MatchIntent", "Awkwardness"):
            assert label not in source, f"{name} references fixture ground truth: {label}"


# --------------------------------------------------------------------------------------
# Branches the happy path does not reach
# --------------------------------------------------------------------------------------


def test_a_structurally_broken_csv_is_rejected_without_raising() -> None:
    """``csv.reader`` in strict mode raises on a misplaced quote; ingestion must not."""
    assert only_defect(payload('psp_1,"quoted"unquoted,capture,1.00,EUR,2026-06-03,,,,x')).code is (
        QuarantineCode.MALFORMED_CSV
    )


def test_a_payload_that_is_only_a_byte_order_mark_is_empty() -> None:
    """After the BOM is stripped there is nothing left — no header, no rows."""
    assert only_defect(b"\xef\xbb\xbf").code is QuarantineCode.EMPTY_PAYLOAD


def test_a_blank_line_inside_the_file_is_skipped_not_treated_as_a_short_row() -> None:
    """A blank line carries no movement. Reporting it as a defect would quarantine a file over
    whitespace; counting it as a row would shift every line number after it."""
    lines, defects = interpret(payload(row(psp_reference="psp_a"), "", row(psp_reference="psp_b")))
    assert not defects
    assert [line.psp_reference for line in lines] == ["psp_a", "psp_b"]
    # The blank line still occupies its position, so numbering follows the file rather than the
    # surviving rows — which is what makes a line number usable against the retained payload.
    assert [line.line_number for line in lines] == [1, 3]


def test_an_invalid_presentment_amount_is_reported_on_its_own_column() -> None:
    defect = only_defect(
        payload(row(presentment_amount="not-a-number", presentment_currency="JPY"))
    )
    assert defect.code is QuarantineCode.INVALID_AMOUNT
    assert defect.column == "presentment_amount"


def test_an_invalid_presentment_currency_is_reported_on_its_own_column() -> None:
    defect = only_defect(payload(row(presentment_amount="100", presentment_currency="jpy")))
    assert defect.code is QuarantineCode.INVALID_CURRENCY
    assert defect.column == "presentment_currency"


def test_an_over_precise_presentment_amount_is_rejected_too() -> None:
    """The presented amount is money and carries the same contract as the settled one."""
    defect = only_defect(payload(row(presentment_amount="1.23456", presentment_currency="JPY")))
    assert defect.code is QuarantineCode.AMOUNT_PRECISION_EXCEEDED


def test_an_absurdly_long_reason_is_truncated_rather_than_stored_whole() -> None:
    """The cap is defensive — the closed code set cannot reach it today — and it is tested so it
    still works on the day a longer column name arrives."""
    defects = [Defect(1, "c" * 200, QuarantineCode.INVALID_AMOUNT) for _ in range(1)]
    defects += [Defect(2, "d" * 400, QuarantineCode.INVALID_AMOUNT)]
    reason = render_reason(defects)
    assert len(reason) == MAX_REASON_LENGTH
    assert reason.endswith("(truncated)")
    assert REASON_ALLOWED.fullmatch(reason), (
        "the truncated form must satisfy the same guard as any other reason"
    )


def test_a_reason_carrying_a_character_outside_the_allowlist_is_refused() -> None:
    """The guard that stops a newline, a quote or an injected fragment reaching a control record."""
    with pytest.raises(ValueError, match="outside the allowlist"):
        render_reason([Defect(1, 'amount"\n<script>', QuarantineCode.INVALID_AMOUNT)])


# --------------------------------------------------------------------------------------
# Corrections from the adversarial review
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("column", ["psp_reference", "merchant_reference"])
def test_a_reference_containing_a_nul_is_quarantined_not_passed_to_the_database(
    column: str,
) -> None:
    """The poison pill: PostgreSQL cannot store U+0000 in a character type.

    A NUL is valid UTF-8, survives ``decode``, survives ``csv.reader``, is not whitespace and is
    under the length limit — so it normalised cleanly and failed at the INSERT with SQLSTATE 22021.
    That turns a quarantine into an unhandled error, and because the receipt is already committed
    and the payload immutable, **every re-delivery reproduces it**: the batch could never reach
    ``parsed`` or ``quarantined``. A single byte permanently jammed its own ingestion.

    Found by four independent review lenses; not by reading the code.
    """
    defect = only_defect(payload(row(**{column: "psp\x00ref"})))
    assert defect.code is QuarantineCode.UNSTORABLE_CHARACTER
    assert defect.column == column


@pytest.mark.parametrize("char", ["\x00", "\x01", "\x1f", "\x7f"])
def test_control_characters_in_a_reference_are_refused(char: str) -> None:
    """NUL is unstorable; the rest are storable and still malformed in a settlement reference —
    and they are exactly the characters that corrupt a log line or a CSV export downstream."""
    assert only_defect(payload(row(psp_reference=f"ref{char}x"))).code is (
        QuarantineCode.UNSTORABLE_CHARACTER
    )


def test_ordinary_punctuation_and_non_ascii_references_are_still_accepted() -> None:
    """The complement: the rule excludes control characters, not everything unusual."""
    for reference in ["ORD/2026-1_a.b", "réf-Ünïcode-01", "ref with spaces"]:
        lines, defects = interpret(payload(row(psp_reference=reference)))
        assert not defects, f"{reference!r} was wrongly refused"
        assert lines[0].psp_reference == reference


def test_the_parse_result_contract_is_what_the_docstring_says() -> None:
    """It claimed "either rows or defects, never both, never neither". Both halves were false.

    A caller trusting that would consume ``rows`` whenever it was non-empty and would take rows
    from a file that had already been condemned. ``ok`` is the only safe gate.
    """
    mixed = parse(payload(row(psp_reference="psp_good"), "short,row"))
    assert mixed.rows and mixed.defects, "one good row and one short row produces both"
    assert not mixed.ok

    empty = parse((HEADER + "\n").encode("utf-8"))
    assert not empty.rows and not empty.defects, "a header-only file produces neither"
    assert empty.ok

    # And the gate holds: interpret() discards the readable row from the defective file.
    lines, defects = interpret(payload(row(psp_reference="psp_good"), "short,row"))
    assert lines == ()
    assert defects


def test_a_quarantine_reason_renders_exactly_as_specified() -> None:
    """The shape tests pin length, ordering and the allowlist. This pins the content.

    Without it, a reason could degrade to something well-formed and useless — the code, the line
    and the column are what make it actionable against the retained payload.
    """
    _, defects = interpret(
        payload(
            row(psp_reference="psp_a"),
            row(psp_reference="psp_b", amount="not-a-number", currency="eur"),
        )
    )
    assert render_reason(list(defects)) == (
        "invalid_amount: line 2, column amount; invalid_currency: line 2, column currency"
    )


def test_a_file_level_defect_renders_without_a_line_or_column() -> None:
    _, defects = interpret(b"")
    assert render_reason(list(defects)) == "empty_payload"
