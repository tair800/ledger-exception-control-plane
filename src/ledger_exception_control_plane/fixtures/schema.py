"""The artifact schema — what a generated corpus *is*, as typed, validated models.

A committed fixture corpus is a boundary: it is written by one increment and read by several
later ones, across a repository boundary in time. NFR-2 requires boundary schemas to be
Pydantic v2 with ``extra="forbid"``, and that applies here for a concrete reason — a corpus
that gained an undeclared field would load silently and diverge silently.

**Scenario metadata is construction intent, never an oracle.** :class:`Scenario` records what
the generator *built*: this line was constructed as a fee split, that one was constructed to
match. It is not the output of running a matcher or a classifier over the data, because those
do not exist yet and, when they do, this metadata is what they will be judged against.
Deriving it from the system under test would make every later test circular.
"""

from __future__ import annotations

import datetime as dt
import decimal
import enum
import uuid
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ledger_exception_control_plane.db.control import ExceptionClassification

#: Bumped when the artifact layout changes in a way a reader must notice. The loader refuses
#: a corpus whose version it does not recognise, rather than reading it optimistically.
FIXTURE_SCHEMA_VERSION: Final = "1"

#: Bumped when the *generated content* would change for an unchanged seed. Committed corpora
#: are pinned by a drift test, so a change here without regenerating fails CI — which is the
#: intended way to notice.
GENERATOR_VERSION: Final = "1.0.0"


class Profile(enum.StrEnum):
    """The closed set of corpus shapes. Deliberately two, not a configuration language.

    ``CANONICAL`` is small, committed, and contains exactly one instance of every scenario in
    the catalogue — it is what tests address by scenario id. ``BULK`` is generated on demand
    at volume and is never committed; it exists so the declared residual distribution can be
    checked as a *distribution*, which a one-of-each corpus cannot demonstrate.
    """

    CANONICAL = "canonical"
    BULK = "bulk"


class ScenarioKind(enum.StrEnum):
    """What condition a scenario constructs.

    Every value maps to something a later increment needs; none is speculative. The first
    three produce lines that a matcher is expected to clear, the rest produce residual.
    """

    EXACT_MATCH = "exact_match"
    REFERENCE_MISMATCH = "reference_mismatch"
    NEAR_AMOUNT_DIFFERENCE = "near_amount_difference"
    PARTIAL_CAPTURE = "partial_capture"
    FEE_SPLIT = "fee_split"
    CHARGEBACK_REVERSAL = "chargeback_reversal"
    FX_ROUNDING = "fx_rounding"
    CROSS_PERIOD_REFUND = "cross_period_refund"
    UNCLASSIFIED = "unclassified"
    MISSING_MERCHANT_REFERENCE = "missing_merchant_reference"
    AMBIGUOUS_MEMO = "ambiguous_memo"
    REPEATED_PSP_REFERENCE = "repeated_psp_reference"


class MatchIntent(enum.StrEnum):
    """What the constructor intended, not what any matcher will decide.

    ``TOLERANCE_POLICY_DEPENDENT`` is the honest third value. Whether a line differing by a
    few minor units clears depends on the tolerance bands OPEN-2 has not settled, so claiming
    either outcome now would be inventing a decision that has not been taken.
    """

    MATCHED = "matched"
    RESIDUAL = "residual"
    TOLERANCE_POLICY_DEPENDENT = "tolerance_policy_dependent"


class Awkwardness(enum.StrEnum):
    """Deliberate imperfections. The plan requires the corpus to be awkward on purpose.

    A matcher validated against tidy input proves nothing, so each of these is a property the
    corpus asserts it contains rather than a defect it tolerates.
    """

    MISSING_MERCHANT_REFERENCE = "missing_merchant_reference"
    UNINFORMATIVE_MEMO = "uninformative_memo"
    AMBIGUOUS_MEMO = "ambiguous_memo"
    REPEATED_PSP_REFERENCE = "repeated_psp_reference"
    SPLIT_ACROSS_ROWS = "split_across_rows"
    CROSS_PERIOD_DATES = "cross_period_dates"
    FOREIGN_PRESENTMENT_CURRENCY = "foreign_presentment_currency"
    NO_LEDGER_COUNTERPART = "no_ledger_counterpart"


CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class _Artifact(BaseModel):
    """Frozen and closed. A corpus is data, and data that can be mutated after validation is
    a corpus that can drift between being read and being loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SettlementLineRecord(_Artifact):
    """One settlement line, in the shape ``settlement_line`` stores.

    ``scenario_id`` is fixture metadata and has no column: it lives in the artifact so a test
    can address a line by the condition it represents, and the loader drops it. Keeping
    generator bookkeeping out of the production relational model is deliberate.
    """

    id: uuid.UUID
    scenario_id: str
    line_number: int = Field(gt=0)
    psp_reference: str = Field(min_length=1, max_length=128)
    merchant_reference: str | None = Field(default=None, max_length=128)
    amount: decimal.Decimal
    currency: CurrencyCode
    value_date: dt.date


class SettlementBatchRecord(_Artifact):
    """A settlement batch and the lines materialised from it.

    ``status`` is ``parsed`` rather than ``received``: the corpus represents the state
    *after* ingestion, because that is the state matching (M2.2) consumes. The raw file is
    retained alongside so M2.1 can be exercised against the same bytes independently — the
    corpus serves both, and neither is derived from the other by any code in this package.
    """

    id: uuid.UUID
    content_hash: Sha256Hex
    source: str = Field(min_length=1, max_length=128)
    received_at: dt.datetime
    status: Literal["parsed"]
    #: POSIX-style path of the raw file, relative to the corpus root. Never an absolute path:
    #: a corpus must not carry the machine that produced it.
    raw_payload_path: str
    lines: tuple[SettlementLineRecord, ...]


class LedgerEntryRecord(_Artifact):
    """One ledger snapshot row, in the shape ``ledger_entry`` stores."""

    id: uuid.UUID
    scenario_id: str
    external_ref: str = Field(min_length=1, max_length=128)
    account_code: str = Field(min_length=1, max_length=64)
    amount: decimal.Decimal
    currency: CurrencyCode
    booked_at: dt.datetime
    description: str | None = None


class Scenario(_Artifact):
    """What one constructed condition is, and why the corpus contains it.

    Every field answers a question the plan requires a reader to be able to answer: what
    condition this represents, which future milestone needs it, and what makes it different
    from the scenario next to it.
    """

    scenario_id: str = Field(pattern=r"^SC-[0-9]{3}-[a-z0-9-]+$")
    kind: ScenarioKind
    intent: MatchIntent
    #: The classification a correct implementation should eventually assign, where the intent
    #: is residual. ``None`` when the line is not intended to be residual at all.
    intended_classification: ExceptionClassification | None
    awkwardness: tuple[Awkwardness, ...]
    why_it_exists: str = Field(min_length=1)
    distinguishing_fields: tuple[str, ...]
    #: Business keys rather than UUIDs, so the metadata is readable and stable under a
    #: namespace change.
    settlement_references: tuple[str, ...]
    ledger_references: tuple[str, ...]


class _Header(_Artifact):
    """Fields every artifact carries so a reader can tell what it is holding."""

    fixture_schema_version: Literal["1"]
    generator_version: str
    profile: Profile
    seed: int


class Corpus(_Header):
    """The input data, and only the input data.

    Scenario metadata lives in :class:`ScenarioCatalogue` rather than here. Keeping them apart
    is not tidiness: the data is what gets loaded into a database and what a future matcher
    consumes, while the metadata is what that matcher will be *judged against*. A reader
    should never be able to reach the answers from the same object that carries the question.
    """

    batches: tuple[SettlementBatchRecord, ...]
    ledger_entries: tuple[LedgerEntryRecord, ...]

    @property
    def line_count(self) -> int:
        return sum(len(batch.lines) for batch in self.batches)


class ScenarioCatalogue(_Header):
    """What the corpus was built to represent. Construction intent, not measured outcome."""

    scenarios: tuple[Scenario, ...]


class InvalidFixture(_Artifact):
    """A deliberately malformed artifact, and the defect it carries.

    These exist for M2.1's quarantine path, which cannot be tested against well-formed input.
    They are **never** part of the loadable corpus: each one would be rejected by the schema
    or has no well-defined normalised form at all, and quietly loading a repaired version of
    it would destroy the only reason it exists.
    """

    path: str
    defect: str = Field(min_length=1)
    why_it_exists: str = Field(min_length=1)


class InvalidCatalogue(_Header):
    """The labelling the invalid artifacts require in order to be safe to keep."""

    fixtures: tuple[InvalidFixture, ...]


class Manifest(_Artifact):
    """Reproducibility record. Deterministic, and carries nothing about the machine.

    No path, no timestamp, no username, no environment: a manifest that varied with the
    machine that produced it would fail the drift check for reasons that have nothing to do
    with the corpus.
    """

    fixture_schema_version: Literal["1"]
    generator_version: str
    profile: Profile
    seed: int
    scenario_count: int
    batch_count: int
    settlement_line_count: int
    ledger_entry_count: int
    #: Intended classification -> number of scenarios, plus ``matched`` and
    #: ``tolerance_policy_dependent`` for the non-residual intents.
    residual_mix: dict[str, int]
    #: SHA-256 over every other artifact in the corpus, in sorted path order.
    content_sha256: Sha256Hex
