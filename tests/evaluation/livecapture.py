"""The live model evaluation: one bounded pass over the golden set, against a real provider (6.4).

Everything else in `tests/evaluation/` grades a run. This module *produces* one, and it is the only
code in the repository that can cause a paid call. Three things follow from that and are built in
rather than documented as intentions.

**It is bounded by a counter that raises.** :class:`~.livetransport.CallBudget` is shared by every
record in the run and refuses the call that would exceed the declared maximum. There is no adaptive
sampling, no "one more if the score looks odd", and no retry outside the transport's own bound. The
plan is printed before the first call and the ceiling is arithmetic, not intent.

**The prompt is built from production fields only, and the harness proves it before dialling.**
:func:`assert_no_answer_leaks` walks every prompt about to be sent and fails the run if any
answer-bearing field name — or any label's actual text — appears in it. The golden record's second
block is never read on the path that builds a prompt; this check exists because "never read" is a
property of code that someone will edit, and a leaked answer would silently turn a measurement into
a lookup.

**The result is marked as captured, and a synthesised cassette is never overwritten.** Live
interactions are written to their own file. ``origin`` is stamped ``captured`` by
:class:`~ledger_exception_control_plane.llm.cassette.RecordingTransport`, which is the only class
permitted to claim one, and the scorer's ``MEASURES_A_MODEL`` set is what turns that into the
difference between a headline that may be quoted and one that may not.

**What this measures, and the limit stated before any number is.** The golden set's labels are a
pure function of the classification — four class rules, applied to 250 records. A model agreeing
with them on 250 rows has agreed with four rules 250 times. The hold-out sub-score is reported
separately because those 25 rows are the ones a person actually confirmed, and ADR-068 already
records that they are worth four independent judgements rather than twenty-five. Neither number
becomes more independent by being computed over more rows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import functools
import json
import pathlib
from collections.abc import Sequence
from typing import Any, Final

import httpx

from ledger_exception_control_plane.classification import (
    SettlementMovement,
    classify,
    movement_type,
)
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.llm.cassette import (
    Interaction,
    RecordingTransport,
    render_cassette,
)
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    ExceptionSubject,
    assemble_evidence,
)
from ledger_exception_control_plane.llm.port import ProviderError, ProviderId
from ledger_exception_control_plane.llm.prompt import (
    PROMPT_CONTRACT_VERSION,
    build_prompt,
    prompt_hash,
)
from ledger_exception_control_plane.llm.providers.openai_tools import OpenAIToolProposer
from ledger_exception_control_plane.llm.schema import ProposalPrompt
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)
from tests.evaluation.golden import (
    GOLDEN_INSTANCES,
    GOLDEN_PROFILE,
    GOLDEN_SCHEMA_VERSION,
    GOLDEN_SEED,
    GoldenRecord,
    GoldenSet,
    load_golden_set,
)
from tests.evaluation.livetransport import (
    API_KEY_VARIABLE,
    BASE_URL_VARIABLE,
    MODEL_VARIABLE,
    CallBudget,
    CallRecord,
    LiveHttpTransport,
    attempts_of,
    credential_from_environment,
    route_of,
    stop_collecting,
)

__all__ = [
    "ANSWER_FIELDS",
    "EVALUATION_VERSION",
    "LIVE_CASSETTE_PATH",
    "LIVE_PROPOSALS_PATH",
    "LIVE_RUN_PATH",
    "LiveOutcome",
    "assert_no_answer_leaks",
    "build_subjects",
    "run_live_evaluation",
]

#: Where a live run writes. Three artefacts, none of which is the synthesised cassette.
_ARTEFACTS: Final = pathlib.Path(__file__).resolve().parents[1] / "golden" / "live"
LIVE_CASSETTE_PATH: Final = _ARTEFACTS / "live-cassette.json"
LIVE_PROPOSALS_PATH: Final = _ARTEFACTS / "live-proposals.jsonl"
LIVE_RUN_PATH: Final = _ARTEFACTS / "live-run.json"

#: Bumped when the harness changes what it sends or how it grades, so a stored run says which
#: harness produced it. The prompt has its own version, and both are recorded.
EVALUATION_VERSION: Final = "1"

#: Every field of a golden record that carries, or explains, the answer. None may reach a prompt.
ANSWER_FIELDS: Final = (
    "expected_treatment",
    "label_rule",
    "label_source",
    "label_why",
    "escalation_is_correct",
    "held_out",
    "human_confirmed_by",
    "human_confirmed_on",
)


@dataclasses.dataclass(frozen=True, slots=True)
class LiveOutcome:
    """What one exception's live call produced. A failure is a result, not a gap."""

    exception_id: str
    #: ``None`` when the provider answered with something that is not a valid proposal, or did not
    #: answer at all. Recorded rather than dropped: a run that silently skipped its failures would
    #: report the schema-valid rate of the calls that happened to work.
    treatment: str | None
    abstained: bool
    confidence: str | None
    #: The port error class name — ``ProviderResponseError`` or ``ProviderUnavailableError``.
    failure: str | None
    #: Kept because the whole point of a malformed-response count is being able to look at one.
    failure_detail: str | None
    prompt_hash: str
    attempts: int
    latency_seconds: float
    reported_model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@functools.cache
def build_subjects() -> dict[str, tuple[ExceptionSubject, list[CandidateEntryFact]]]:
    """Rebuild every golden subject from the same seeded corpus the golden set was generated from.

    Cached because it is pure: the corpus is a function of a committed seed, profile and instance
    count, and regenerating 1,200 instances per caller turned a test file into a slow one. The
    cache is keyed on nothing because the function takes nothing — if it ever grows an argument,
    that is the moment to check the cache is still correct.

    The same chain ``golden.build_golden_set`` runs — generate, match, classify — stopped one step
    earlier, at the point where a subject exists and a label does not. **Nothing here reads a
    golden record**, so there is no path by which an answer could be carried into a prompt; the
    join back to the labels happens after the model has answered, on ``exception_id``.
    """
    corpus = generate(GOLDEN_SEED, GOLDEN_PROFILE, GOLDEN_INSTANCES)
    rows = {row.id: row for batch in corpus.corpus.batches for row in batch.lines}
    entries = {entry.id: entry for entry in corpus.corpus.ledger_entries}

    outcome = match(
        [
            CandidateLine(r.id, r.line_number, r.amount, r.currency, r.value_date)
            for r in rows.values()
        ],
        [
            CandidateEntry(e.id, e.external_ref, e.amount, e.currency, e.booked_at.date())
            for e in entries.values()
        ],
        DEFAULT_POLICY,
    )
    matched = {pair.line_id for pair in outcome.matches}
    consumed = {pair.entry_id for pair in outcome.matches}

    movements = [
        SettlementMovement(
            r.id,
            r.merchant_reference,
            movement_type(r.transaction_type),
            r.amount,
            r.currency,
            r.value_date,
            r.id in matched,
        )
        for r in rows.values()
    ]
    by_id = {m.id: m for m in movements}

    unconsumed = [
        CandidateEntryFact(
            entry_id=e.id,
            external_ref=e.external_ref,
            account_code=e.account_code,
            amount=e.amount,
            currency=e.currency,
            booked_on=e.booked_at.date(),
            description=e.description,
        )
        for e in entries.values()
        if e.id not in consumed
    ]

    subjects: dict[str, tuple[ExceptionSubject, list[CandidateEntryFact]]] = {}
    for decision in classify([m for m in movements if not m.matched], movements):
        movement = by_id[decision.line_id]
        row = rows[decision.line_id]
        subjects[str(decision.line_id)] = (
            ExceptionSubject(
                exception_id=decision.line_id,
                classification=decision.classification,
                settlement_line_id=decision.line_id,
                psp_reference=row.psp_reference,
                merchant_reference=movement.merchant_reference,
                transaction_type=row.transaction_type,
                amount=movement.amount,
                currency=movement.currency,
                value_date=movement.value_date,
            ),
            unconsumed,
        )
    return subjects


def assert_no_answer_leaks(
    prompts: dict[str, ProposalPrompt], records: Sequence[GoldenRecord]
) -> None:
    """Refuse to send a prompt that carries any part of the answer.

    Two checks, because they fail differently. The **field names** catch a future edit that starts
    interpolating a golden record into the evidence document. The **label text** catches the subtler
    one: a rationale or a rule sentence copied into a prompt under some other key, where the field
    name would not appear at all.
    """
    labelled = {record.exception_id: record for record in records}
    offences: list[str] = []
    for exception_id, prompt in prompts.items():
        blob = f"{prompt.system}\n{prompt.user}"
        offences += [
            f"{exception_id}: field name {field!r}" for field in ANSWER_FIELDS if field in blob
        ]
        record = labelled.get(exception_id)
        if record is None:
            continue
        for field in ("label_why", "label_rule", "expected_treatment"):
            value = str(getattr(record, field))
            # `expected_treatment` values are also legitimate vocabulary in the system policy, so
            # only the evidence document is searched for those; the policy is a constant this
            # module cannot influence.
            haystack = prompt.user if field == "expected_treatment" else blob
            if value and value in haystack:
                offences.append(f"{exception_id}: the value of {field}")
    if offences:
        raise RuntimeError(
            "refusing to make a live call: the prompt carries part of the answer key.\n  "
            + "\n  ".join(offences)
        )


def plan(records: Sequence[GoldenRecord], max_attempts: int) -> dict[str, Any]:
    """The non-secret run metadata, printed before the first call. No value, only names."""
    credential = credential_from_environment()
    return {
        "route": route_of(credential.base_url),
        "route_variable": BASE_URL_VARIABLE,
        "model_alias": credential.model_alias,
        "model_variable": MODEL_VARIABLE,
        "credential_variable": API_KEY_VARIABLE,
        "records": len(records),
        "maximum_calls": len(records) * max_attempts,
        "maximum_attempts_per_record": max_attempts,
        "timeout_seconds": 120.0,
        "golden_schema_version": GOLDEN_SCHEMA_VERSION,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "artefacts": {
            "cassette": str(LIVE_CASSETTE_PATH),
            "proposals": str(LIVE_PROPOSALS_PATH),
            "run": str(LIVE_RUN_PATH),
        },
    }


async def run_live_evaluation(
    golden: GoldenSet | None = None,
    *,
    max_attempts: int = 3,
    concurrency: int = 4,
) -> dict[str, Any]:
    """Make the run. Every call is counted, every outcome is kept, nothing is retried adaptively."""
    golden = golden or load_golden_set()
    records = golden.records
    subjects = build_subjects()

    missing = [r.exception_id for r in records if r.exception_id not in subjects]
    if missing:
        raise RuntimeError(
            f"{len(missing)} golden records have no rebuilt subject; the corpus drifted"
        )

    prompts = {r.exception_id: build_prompt(*_pack(subjects[r.exception_id])) for r in records}
    assert_no_answer_leaks(prompts, records)

    credential = credential_from_environment()
    budget = CallBudget(maximum=len(records) * max_attempts)
    started_at = dt.datetime.now(dt.UTC)

    async with httpx.AsyncClient() as client:
        transport = LiveHttpTransport(
            client,
            base_url=credential.base_url,
            api_key=credential.key,
            budget=budget,
            max_attempts=max_attempts,
        )
        recorder = RecordingTransport(
            transport,
            provider=ProviderId.OPENAI,
            model_id=credential.model_alias,
            model_version=OpenAIToolProposer(
                transport, model_id=credential.model_alias
            ).model_version,
        )
        proposer = OpenAIToolProposer(recorder, model_id=credential.model_alias)

        gate = asyncio.Semaphore(concurrency)

        async def one(record: GoldenRecord) -> LiveOutcome:
            async with gate:
                prompt = prompts[record.exception_id]
                # This task's own attempts. Not a slice of the transport's shared list: two
                # records in flight interleave there, and the first version of this reported a
                # retry that had not happened.
                mine: list[CallRecord] = []
                token = attempts_of(mine)
                failure = detail = None
                treatment = confidence = None
                abstained = False
                try:
                    proposal = await proposer.propose(prompt)
                    treatment = proposal.treatment.value
                    confidence = proposal.confidence.value
                    abstained = proposal.abstained
                except ProviderError as exc:
                    failure = type(exc).__name__
                    detail = str(exc)[:400]
                finally:
                    stop_collecting(token)
                return LiveOutcome(
                    exception_id=record.exception_id,
                    treatment=treatment,
                    abstained=abstained,
                    confidence=confidence,
                    failure=failure,
                    failure_detail=detail,
                    prompt_hash=prompt_hash(prompt),
                    attempts=len(mine),
                    latency_seconds=sum(c.latency_seconds for c in mine),
                    reported_model=next(
                        (c.reported_model for c in reversed(mine) if c.reported_model), None
                    ),
                    prompt_tokens=_last(mine, "prompt_tokens"),
                    completion_tokens=_last(mine, "completion_tokens"),
                    total_tokens=_last(mine, "total_tokens"),
                )

        outcomes = list(await asyncio.gather(*(one(record) for record in records)))

    finished_at = dt.datetime.now(dt.UTC)
    _write(
        outcomes,
        recorder.recorded,
        budget,
        started_at,
        finished_at,
        credential.model_alias,
        credential.base_url,
    )
    return {
        "outcomes": outcomes,
        "calls": transport.calls,
        "interactions": recorder.recorded,
        "budget": budget,
    }


def _pack(pair: tuple[ExceptionSubject, list[CandidateEntryFact]]) -> tuple[ExceptionSubject, Any]:
    subject, candidates = pair
    return subject, assemble_evidence(subject, candidates, DEFAULT_POLICY)


def _last(calls: Sequence[Any], field: str) -> int | None:
    return next((getattr(c, field) for c in reversed(calls) if getattr(c, field) is not None), None)


def _write(
    outcomes: Sequence[LiveOutcome],
    interactions: Sequence[Interaction],
    budget: CallBudget,
    started_at: dt.datetime,
    finished_at: dt.datetime,
    model_alias: str,
    base_url: str,
) -> None:
    """Three artefacts: what to grade, what to replay, and what the run was."""
    _ARTEFACTS.mkdir(parents=True, exist_ok=True)

    # 1. Scoreable proposals. Failures are omitted, so the scorer reports them as `unanswered`
    #    rather than being handed a treatment nobody proposed.
    LIVE_PROPOSALS_PATH.write_text(
        "".join(
            json.dumps(
                {
                    "exception_id": o.exception_id,
                    "treatment": o.treatment,
                    "abstained": o.abstained,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for o in outcomes
            if o.treatment is not None
        ),
        encoding="utf-8",
        newline="\n",
    )

    # 2. The cassette, in the repository's own format so `ReplayTransport` can replay it with no
    #    special case. Its own file: the synthesised corpus is not touched.
    LIVE_CASSETTE_PATH.write_text(render_cassette(interactions), encoding="utf-8", newline="\n")

    # 3. The run itself. Enough to reproduce and inspect; no credential, and the route is scrubbed.
    reported = sorted({o.reported_model for o in outcomes if o.reported_model})
    LIVE_RUN_PATH.write_text(
        json.dumps(
            {
                "evaluation_version": EVALUATION_VERSION,
                "prompt_contract_version": PROMPT_CONTRACT_VERSION,
                "golden_schema_version": GOLDEN_SCHEMA_VERSION,
                "origin": "captured",
                "route": route_of(base_url),
                "model_alias": model_alias,
                "models_reported_by_the_route": reported,
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "records": len(outcomes),
                "calls_made": budget.spent,
                "call_budget": budget.maximum,
                "outcomes": [dataclasses.asdict(o) for o in outcomes],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
