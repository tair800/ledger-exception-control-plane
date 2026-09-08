"""The human-label packet for §20's hold-out slice, and the validator that imports it back.

`PROJECT_SPEC.md` §20 requires *"a human-labelled hold-out slice"*. ADR-060 recorded the honest
state of that requirement as **OPEN-15**: the slice is selected deterministically and every record
says ``label_source: derived``, because no person has confirmed one. This module is the mechanism
that lets a person confirm them — and, more importantly, the mechanism that stops anything else
being presented as though a person had.

**Three rules, and they are the reason the module exists.**

1. **No label is ever generated here.** Nothing in this file produces a treatment. It writes a
   packet with a blank column and it reads a file somebody filled in. A generator that could fill
   the column would eventually be used to fill it, and §20's hold-out exists precisely to catch a
   wrong label table — a slice labelled by the same rules it is meant to audit audits nothing.

2. **A synthetic import can never be described as human.** Synthetic rows are needed to test this
   validator's own mechanics, so the format carries an explicit ``synthetic`` marker,
   :attr:`LabelImport.label_source` *raises* rather than returning
   :attr:`~tests.evaluation.labels.LabelSource.HUMAN` for one, and
   :attr:`LabelImport.is_evaluation_evidence` is ``False``. Marking a machine's answers ``human``
   would be inventing provenance, which `CLAUDE.md` §10 forbids and which is worse than the gap it
   would hide.

3. **The packet carries no ground truth.** Not the expected treatment, not the label rule, not the
   label's reasoning, not the corpus's ``scenario_id``, not the cassette's stand-in treatment, not
   a model's proposal. :data:`PERMITTED_FIELDS` is an allowlist rather than a denylist, so a field
   added to :class:`~tests.evaluation.golden.GoldenRecord` is absent from the packet until somebody
   decides it is safe to show, and a test asserts the committed packet contains none of the
   forbidden names.

**The account policy is deliberately withheld, and that is the least obvious decision here.** For
two of the four reachable classes the derived label follows mechanically from one fact: the account
policy configures nothing for them, so the correct action is to escalate. A labeller handed that
table would reproduce the derived label rather than test it, and the hold-out would agree with the
generator by construction. So the packet gives the labeller the exception's facts and what each
treatment *means* as an accounting action, and asks for their own judgement. Disagreement with the
derived label is the useful outcome, not a defect in the packet.

**The slice is frozen and versioned.** ``HOLD_OUT_VERSION`` plus a digest over exactly what a
labeller was shown. An import must declare both, and a mismatch is refused: labels confirmed against
one set of facts must never be attached to a different one, and a regenerated corpus is exactly how
that happens quietly.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import json
import pathlib
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from ledger_exception_control_plane.db.control import TreatmentCode
from tests.evaluation.golden import GoldenRecord, GoldenSet, load_golden_set
from tests.evaluation.labels import LabelSource

__all__ = [
    "BLANK_COLUMNS",
    "FORBIDDEN_IN_A_PACKET",
    "HOLD_OUT_VERSION",
    "LABEL_DEFINITIONS",
    "PACKET_CSV",
    "PACKET_DIR",
    "PACKET_JSONL",
    "PACKET_README",
    "PERMITTED_FIELDS",
    "ImportRejected",
    "ImportedLabel",
    "LabelImport",
    "Packet",
    "build_packet",
    "hold_out_digest",
    "read_import",
    "render_packet_csv",
    "render_packet_jsonl",
    "render_packet_readme",
    "validate_import",
]

#: Bumped when the frozen slice changes — different records, or a change to what a labeller is
#: shown. An import declaring another version is refused rather than migrated, because a label is a
#: judgement about a specific set of facts and cannot be carried across a change to them.
HOLD_OUT_VERSION: Final = "1"

PACKET_DIR: Final = pathlib.Path(__file__).resolve().parents[1] / "golden" / "human-label-packet"

#: The file the owner fills in. CSV because a spreadsheet is what a person confirming 25 rows will
#: actually open.
PACKET_CSV: Final = PACKET_DIR / "hold-out-slice.csv"

#: The machine-readable companion, with the packet's metadata on a header line — the same shape the
#: golden set uses, for the same reason: provenance that can be separated from the artefact will be.
PACKET_JSONL: Final = PACKET_DIR / "hold-out-slice.jsonl"

#: The instructions. Generated rather than hand-written, so the definitions a labeller reads and the
#: vocabulary the validator enforces cannot drift apart.
PACKET_README: Final = PACKET_DIR / "README.md"

#: **The allowlist.** Exactly the golden-record fields a labeller may see, in packet order.
#:
#: An allowlist and not a denylist, deliberately. A field added to ``GoldenRecord`` — a second
#: label rule, a confidence, a reviewer note — is absent from the packet until somebody adds it
#: here, which is a decision somebody makes rather than a leak somebody discovers.
PERMITTED_FIELDS: Final = (
    "exception_id",
    "classification",
    "transaction_type",
    "amount",
    "currency",
    "value_date",
    "settlement_period",
    "originating_period",
    "has_merchant_reference",
)

#: Field names that must never appear as a **column or key** in any packet file.
#:
#: Names rather than substrings, because the packet's own instructions have to be able to say which
#: things are deliberately withheld — a text scan for the word "proposal" would flag the sentence
#: explaining that no proposal is included. The value-level leak check is separate and stronger: a
#: test asserts that no held-out record's label rule or stated reasoning appears anywhere in the
#: packet, in any file, and that no row carries its own expected treatment.
#:
#: The first five are the answer key the labeller is meant to be independent of; the rest are the
#: corpus's construction metadata and the two other sources of an answer — a model's proposal, and
#: the cassette's positional stand-in.
FORBIDDEN_IN_A_PACKET: Final = frozenset(
    {
        "expected_treatment",
        "label_rule",
        "label_why",
        "label_source",
        "escalation_is_correct",
        "scenario_id",
        "intended_classification",
        "match_intent",
        "awkwardness",
        "proposal",
        "proposed_treatment",
        "cassette_id",
        "stand_in",
        "rationale",
    }
)

#: The columns a person fills in. Written empty, and never written by this module.
BLANK_COLUMNS: Final = ("HUMAN_LABEL", "HUMAN_NOTE", "labelled_by", "labelled_on")

#: Columns that travel with the packet so an import cannot be checked against the wrong slice.
PROVENANCE_COLUMNS: Final = ("hold_out_version", "hold_out_sha256", "synthetic")

#: What each treatment *means* as an accounting action. Deliberately not "when it is priceable":
#: that is the account policy, and the account policy is the derivation this slice audits.
LABEL_DEFINITIONS: Final[Mapping[str, str]] = {
    TreatmentCode.REBOOK.value: (
        "Post the movement the ledger is missing, recognised in the accounting period the "
        "settlement line itself settled in."
    ),
    TreatmentCode.ACCRUE.value: (
        "Recognise the same movement in the period it economically belongs to — the period of the "
        "movement it reverses. Needs a known originating period; without one there is nothing to "
        "accrue into."
    ),
    TreatmentCode.WRITE_OFF.value: (
        "Recognise the residual as a loss rather than as the movement it appeared to be."
    ),
    # **The trailing clause was removed after an audit named it.** It used to read "for some
    # conditions it is the only correct one", which is not the accounting meaning of escalate — the
    # first sentence already gives that. It is an assertion about the *answer key*: that a set of
    # conditions exists where escalate is uniquely right. An attacker reading the packet alone
    # joined it to the withheld-policy note and reconstructed most of the slice from the two.
    # Neither sentence helped a labeller decide anything, so both were cut rather than defended.
    TreatmentCode.ESCALATE.value: (
        "Refer the case to a person because it cannot be resolved from the facts shown. This is a "
        "real answer and not a failure to answer."
    ),
}

#: The allowed vocabulary, as one field a spreadsheet can show on every row.
ALLOWED_LABELS: Final = "|".join(sorted(code.value for code in TreatmentCode))

_YES: Final = frozenset({"yes", "true", "1", "y"})
_NO: Final = frozenset({"no", "false", "0", "n", ""})


class ImportRejected(Exception):  # noqa: N818 - a refusal, not an error condition
    """An import that must not be used. Carries every reason, not the first one.

    Named for what it *is* rather than with an ``Error`` suffix, deliberately: a rejected import is
    an expected outcome of a human process — a spreadsheet with three blanks in it — and calling it
    ``ImportError`` would both shadow a builtin and suggest something went wrong in the code.

    Every reason, because a reviewer fixing a returned spreadsheet needs the whole list; a validator
    that stops at the first missing label makes them run it twenty-five times.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("; ".join(self.reasons))


@dataclasses.dataclass(frozen=True, slots=True)
class Packet:
    """The frozen hold-out slice, reduced to what a labeller may see."""

    hold_out_version: str
    records: tuple[Mapping[str, str], ...]

    @property
    def exception_ids(self) -> frozenset[str]:
        return frozenset(row["exception_id"] for row in self.records)

    @property
    def digest(self) -> str:
        """SHA-256 over exactly what a labeller was shown, canonically serialised.

        Over the *shown* fields rather than over the whole golden record, so the digest answers the
        question an import needs answered: were these labels formed against these facts. A digest
        over fields the labeller never saw would refuse a valid import for a change that could not
        have affected their judgement.
        """
        payload = json.dumps(
            {"version": self.hold_out_version, "records": [dict(r) for r in self.records]},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class ImportedLabel:
    """One label a person wrote, with who wrote it and when."""

    exception_id: str
    treatment: TreatmentCode
    note: str
    labelled_by: str
    labelled_on: str


@dataclasses.dataclass(frozen=True, slots=True)
class LabelImport:
    """A validated import. Constructing one does not make it human — see :attr:`label_source`."""

    hold_out_version: str
    hold_out_digest: str

    #: Declared in the file. ``True`` means these rows were produced to exercise this validator and
    #: are not evidence about anything.
    synthetic: bool

    labels: Mapping[str, ImportedLabel]

    @property
    def is_evaluation_evidence(self) -> bool:
        """Whether these labels may appear in any reported figure. ``False`` for synthetic."""
        return not self.synthetic

    @property
    def label_source(self) -> LabelSource:
        """``HUMAN``, and **only** for a non-synthetic import.

        Raises for a synthetic one rather than returning ``DERIVED`` or a third value. A synthetic
        import has no label source at all: nobody derived those labels and nobody confirmed them,
        so any answer here would be a claim about provenance that is not true. Refusing is the only
        response that cannot be misread by a caller that ignores a flag.
        """
        if self.synthetic:
            raise ValueError(
                "this import is marked synthetic: its labels were produced to exercise the "
                "validator and have no label source. They must never be reported as human, "
                "counted in an accuracy figure, or written into the golden set."
            )
        return LabelSource.HUMAN


def _permitted_view(record: GoldenRecord) -> dict[str, str]:
    """One golden record, reduced to the allowlist and rendered as text.

    Text throughout, because the packet is a CSV a person edits and a JSONL a validator reads, and
    two representations of the same value are two ways for the digest to disagree with itself.
    ``None`` becomes the empty string, which the packet's README explains as "not established".
    """
    view: dict[str, str] = {}
    for field in PERMITTED_FIELDS:
        value = getattr(record, field)
        if value is None:
            view[field] = ""
        elif isinstance(value, bool):
            view[field] = "yes" if value else "no"
        else:
            view[field] = str(value)
    return view


def build_packet(golden: GoldenSet | None = None) -> Packet:
    """The frozen slice: the golden set's held-out records, reduced to the allowlist.

    Sorted by exception id, so the packet's order is a property of the data and the digest is
    stable across runs and platforms.
    """
    source = golden or load_golden_set()
    held = sorted(source.hold_out, key=lambda record: record.exception_id)
    if not held:
        raise ValueError("the golden set holds no held-out records; there is nothing to review")
    return Packet(
        hold_out_version=HOLD_OUT_VERSION,
        records=tuple(_permitted_view(record) for record in held),
    )


def render_packet_csv(packet: Packet) -> str:
    """The spreadsheet. LF newlines and no BOM, so the committed bytes are the same everywhere."""
    columns = [*PERMITTED_FIELDS, "allowed_labels", *BLANK_COLUMNS, *PROVENANCE_COLUMNS]
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in packet.records:
        writer.writerow(
            {
                **row,
                "allowed_labels": ALLOWED_LABELS,
                **dict.fromkeys(BLANK_COLUMNS, ""),
                "hold_out_version": packet.hold_out_version,
                "hold_out_sha256": packet.digest,
                "synthetic": "no",
            }
        )
    return buffer.getvalue()


def render_packet_jsonl(packet: Packet) -> str:
    """The machine-readable companion: metadata on line one, one record per line after it."""
    header = {
        "hold_out_version": packet.hold_out_version,
        "hold_out_sha256": packet.digest,
        "records": len(packet.records),
        "allowed_labels": sorted(code.value for code in TreatmentCode),
        "label_definitions": dict(LABEL_DEFINITIONS),
        "shown_fields": list(PERMITTED_FIELDS),
        "what_this_is": (
            "Hold-out slice for independent human labelling (PROJECT_SPEC.md section 20). It "
            "carries no expected treatment, no label rule, no model proposal and none of the "
            "corpus's construction metadata: the point is a judgement formed without them. The "
            "account policy is deliberately not included either — it is the rule the derived "
            "labels follow, and a labeller shown it would reproduce those labels rather than test "
            "them. Fill in HUMAN_LABEL, HUMAN_NOTE, labelled_by and labelled_on. No label in this "
            "file was produced by any program."
        ),
    }
    lines = [json.dumps(header, sort_keys=True, separators=(",", ":"))]
    for row in packet.records:
        lines.append(
            json.dumps(
                {
                    **row,
                    "HUMAN_LABEL": None,
                    "HUMAN_NOTE": None,
                    "labelled_by": None,
                    "labelled_on": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    return "\n".join(lines) + "\n"


def render_packet_readme(packet: Packet) -> str:
    """The instructions, generated from the same constants the validator enforces."""
    definitions = "\n".join(
        f"| `{code}` | {LABEL_DEFINITIONS[code]} |" for code in sorted(LABEL_DEFINITIONS)
    )
    shown = "\n".join(f"- `{field}`" for field in PERMITTED_FIELDS)
    return f"""# Hold-out label packet — version {packet.hold_out_version}

{len(packet.records)} exceptions from the committed golden set, for **independent** human labelling
(`PROJECT_SPEC.md` §20). Generated by `make label-packet`; never edited by hand.

`hold_out_sha256` = `{packet.digest}`

## What to do

Open `hold-out-slice.csv`. For each row, decide which single treatment is the correct accounting
action for the exception as described, and write it in `HUMAN_LABEL`. Put your reasoning in
`HUMAN_NOTE` where it is not obvious — especially where you think the answer is arguable. Fill in
`labelled_by` and `labelled_on` (ISO date). Leave every other column alone.

Then run:

```
uv run python -m tests.evaluation import-labels <your-file.csv>
```

It validates and reports. It never writes a label, and it refuses the file rather than repairing it.

## The vocabulary

Exactly one of these per row. Nothing else is accepted.

| Label | What it means as an accounting action |
|---|---|
{definitions}

## What you are shown, and what you are not

Shown:

{shown}

An empty `originating_period` means no single counterpart movement was established — not that one
does not exist.

**Deliberately withheld:** the expected treatment, the rule that produced it and its reasoning; any
model's proposal; the corpus's construction metadata; and **the account policy** — the table mapping
a classification to a ledger account. The last one is the least obvious and the most important: it
is an input to the derived labels this slice exists to test, so a labeller who had it would be
re-running the derivation instead of checking it. Judge each case on its own facts.

**If two labels would post the same thing.** Where the period a treatment would recognise the
movement in is the same under two labels, the evidence cannot separate them. Pick the one whose
*reason* fits and say so in `HUMAN_NOTE`. No tie-break rule is given here, because a rule would be
the answer for the rows it applies to.

**Disagreement is the useful outcome.** If your label differs from the derived one, that is a
finding to argue about, not an error in your row.

## What happens to a synthetic file

The format carries a `synthetic` column so this validator's own mechanics can be tested without a
person. A file marked synthetic is validated and then refused a label source: it can never be
reported as human, counted in an accuracy figure, or written into the golden set.
"""


def _row_flag(name: str, value: str, row: int, reasons: list[str]) -> bool:
    text = value.strip().lower()
    if text in _YES:
        return True
    if text in _NO:
        return False
    reasons.append(f"row {row}: {name} is {value!r}, which is neither yes nor no")
    return False


def _read_csv(text: str) -> list[dict[str, str]]:
    return [
        {key: (value or "") for key, value in row.items() if key is not None}
        for row in csv.DictReader(io.StringIO(text, newline=""))
    ]


def _read_jsonl(text: str) -> list[dict[str, Any]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    header = json.loads(lines[0])
    rows: list[dict[str, Any]] = []
    for line in lines[1:]:
        row = json.loads(line)
        row.setdefault("hold_out_version", header.get("hold_out_version", ""))
        row.setdefault("hold_out_sha256", header.get("hold_out_sha256", ""))
        row.setdefault("synthetic", header.get("synthetic", "no"))
        rows.append(row)
    return rows


def validate_import(rows: Iterable[Mapping[str, Any]], packet: Packet) -> LabelImport:
    """Check a returned file against the frozen slice, and refuse it if anything is wrong.

    Six checks, and each one is a way a label set can be wrong while looking complete:

    * the id set must match the packet **exactly** — a subset scores only the rows somebody found
      easy, and an extra id is a label for a record nobody was asked about;
    * no duplicates — two answers for one record is not a label, it is a disagreement, and silently
      keeping the last row decides it by row order;
    * every label present — a blank is not an abstention, it is an unanswered question, and
      ``escalate`` is a real answer that a person has to actually choose;
    * every label in the vocabulary — ``REBOOK ``, ``re-book`` and ``rebook?`` are refused rather
      than guessed at;
    * attribution on every row, for a non-synthetic import — ADR-060 requires a human label to be
      recorded "with who and when", and a human label with nobody attached is the same gap it was
      meant to close;
    * the declared version and digest must match the frozen slice — labels formed against one set
      of facts must never be attached to another.

    Raises :class:`ImportRejected` with every reason. Never returns a partially valid import: a
    caller handed one would use it.
    """
    reasons: list[str] = []
    material = list(rows)
    if not material:
        raise ImportRejected(["the import is empty"])

    versions = {str(row.get("hold_out_version", "")).strip() for row in material}
    digests = {str(row.get("hold_out_sha256", "")).strip() for row in material}
    synthetic_flags = {
        _row_flag("synthetic", str(row.get("synthetic", "")), index + 1, reasons)
        for index, row in enumerate(material)
    }

    if len(versions) != 1 or len(digests) != 1:
        reasons.append(
            f"the rows declare {len(versions)} hold-out version(s) and {len(digests)} digest(s); "
            "one file describes one slice"
        )
    if len(synthetic_flags) != 1:
        reasons.append(
            "some rows are marked synthetic and some are not; a file is either a person's work or "
            "it is not"
        )

    declared_version = next(iter(versions)) if len(versions) == 1 else ""
    declared_digest = next(iter(digests)) if len(digests) == 1 else ""
    synthetic = next(iter(synthetic_flags)) if len(synthetic_flags) == 1 else True

    if declared_version != packet.hold_out_version:
        reasons.append(
            f"the import declares hold-out version {declared_version!r} and the frozen slice is "
            f"{packet.hold_out_version!r}; regenerate the packet and label it again"
        )
    if declared_digest != packet.digest:
        reasons.append(
            f"the import declares digest {declared_digest[:12]!r} and the frozen slice hashes to "
            f"{packet.digest[:12]!r}; these labels were formed against different facts"
        )

    labels: dict[str, ImportedLabel] = {}
    seen: set[str] = set()
    for index, row in enumerate(material, start=1):
        exception_id = str(row.get("exception_id", "")).strip()
        if not exception_id:
            reasons.append(f"row {index}: no exception_id")
            continue
        if exception_id in seen:
            reasons.append(f"row {index}: {exception_id} appears more than once")
            continue
        seen.add(exception_id)

        raw = row.get("HUMAN_LABEL")
        label = ("" if raw is None else str(raw)).strip()
        if not label:
            reasons.append(f"row {index}: {exception_id} has no HUMAN_LABEL")
            continue
        try:
            treatment = TreatmentCode(label)
        except ValueError:
            reasons.append(
                f"row {index}: {exception_id} is labelled {label!r}, which is not one of "
                f"{ALLOWED_LABELS}"
            )
            continue

        by = str(row.get("labelled_by") or "").strip()
        on = str(row.get("labelled_on") or "").strip()
        if not synthetic and not (by and on):
            reasons.append(
                f"row {index}: {exception_id} has no labelled_by/labelled_on; a human label is "
                "recorded with who confirmed it and when, or it is not a human label"
            )
            continue

        labels[exception_id] = ImportedLabel(
            exception_id=exception_id,
            treatment=treatment,
            note=str(row.get("HUMAN_NOTE") or "").strip(),
            labelled_by=by,
            labelled_on=on,
        )

    expected = packet.exception_ids
    missing = sorted(expected - seen)
    unknown = sorted(seen - expected)
    if missing:
        reasons.append(f"{len(missing)} record(s) of the frozen slice are absent: {missing[:3]}")
    if unknown:
        reasons.append(f"{len(unknown)} label(s) are for records not in the slice: {unknown[:3]}")

    if reasons:
        raise ImportRejected(reasons)

    return LabelImport(
        hold_out_version=declared_version,
        hold_out_digest=declared_digest,
        synthetic=synthetic,
        labels=labels,
    )


def read_import(path: pathlib.Path, packet: Packet | None = None) -> LabelImport:
    """Read and validate a returned workbook, CSV or JSONL. Refuses anything else by extension.

    ``.xlsx`` is read through :mod:`tests.evaluation.workbook`, imported lazily so that the CSV and
    JSONL paths — the ones CI exercises — keep working with no spreadsheet library installed.
    """
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        from tests.evaluation.workbook import read_workbook_rows

        try:
            rows: list[Mapping[str, Any]] = list(read_workbook_rows(path))
        except (KeyError, ValueError, OSError, zipfile.BadZipFile) as unreadable:
            # A refusal, not a traceback. A file that is not a workbook, or a workbook without the
            # records sheet, is the same kind of mistake as a missing label — the owner attached
            # the wrong file — and it should read like one.
            raise ImportRejected(
                [f"{path.name}: not a readable label workbook ({unreadable})"]
            ) from unreadable
        return validate_import(rows, packet or build_packet())

    text = path.read_text(encoding="utf-8-sig")
    if suffix == ".csv":
        rows = list(_read_csv(text))
    elif suffix in {".jsonl", ".json"}:
        rows = list(_read_jsonl(text))
    else:
        raise ImportRejected(
            [f"{path.name}: expected a .xlsx, .csv or .jsonl file, not {path.suffix!r}"]
        )
    return validate_import(rows, packet or build_packet())


def hold_out_digest() -> str:
    """The frozen slice's digest, for a caller that only needs the number."""
    return build_packet().digest
