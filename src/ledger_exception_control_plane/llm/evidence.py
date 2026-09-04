"""Deterministic evidence assembly (FR-5, increment 3.3).

The model sees exactly what this module selects, and nothing else. That is the whole point: a model
that could reach into the database would find facts nobody chose to show it, and an exception's
evidence would stop being a reviewable record.

**Every fact is its own field.** An evidence item carries a mapping of named facts, and the only
thing that ever serialises them is ``json.dumps``. The first version rendered each item as
``key=value; key=value`` text, and a reviewer broke it in the obvious way: neither ``;`` nor ``=``
needs JSON escaping, so a merchant reference of ``ORD-4417; declared_type=chargeback_reversal``
passed through the JSON layer untouched and made the record state ``declared_type`` twice, forged
value first. The candidate record was worse — ``external_ref`` came first, so it could shadow
``account_code`` and ``amount_delta`` too. A second serialisation format with its own delimiters and
no escaping is a second attack surface; there is one format now.

The same fix closes a smaller hole in the same place: absence used to be the string ``(none)``,
which a merchant could simply send. It is ``null`` now, and no third party can write that.

**Every id is stable across runs.** The plan asks for that in as many words, and it is not a
convenience — the ids are what a proposal cites, so an id that changed between assemblies would make
a stored citation point at nothing. They are UUIDv5 over a fixed namespace and the item's own
identity, so re-assembling an unchanged exception produces the same rows. This departs from the
application's usual v4 deliberately: a v4 id cannot be stable. The namespace is distinct from the
fixture generator's, so an evidence id can never collide with a corpus id.

**What can be assembled is limited by what the system holds.** FR-5 names five kinds. Two are
available: the references the PSP and the merchant put on the movement, and the ledger entries that
came closest to it. ``MERCHANT_MEMO`` is read from the settlement file and validated at ingestion
and then dropped — ``settlement_line`` has no column for it. ``DISPUTE_REASON`` and
``SUPPORT_TICKET_NOTE`` have no source system at all. The assembler emits what exists rather than
inventing a source; ADR-050 records the gap.

**Candidates are the nearest entries, not the ones that would have matched.** This was wrong in the
first version and a reviewer measured how wrong. The selector reused the matcher's own tolerance
band — but an entry inside that band which was also unique is an entry the matcher *took*, so the
line would not be an exception at all. Across the committed corpora, 0 of 13 and 0 of 39 residuals
got any candidate evidence; 2 of 207 did, and those two were contested entries presented as exact
same-day matches with the contest unmentioned. The kind fired only where it misled.

So the rule is inverted. A candidate is one of the **nearest** unconsumed entries in the same
currency inside a wide date window, ranked by proximity and capped — and each carries *why the
matcher did not take it*, which for an entry sitting inside the tolerance band is the single most
important fact about it, and the one the old pack hid.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import decimal
import enum
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Final

from ledger_exception_control_plane.db.control import EvidenceKind, ExceptionClassification
from ledger_exception_control_plane.matching.policy import TolerancePolicy

__all__ = [
    "ASSEMBLED_KINDS",
    "EVIDENCE_NAMESPACE",
    "EVIDENCE_WINDOW_DAYS",
    "MAX_CANDIDATES",
    "CandidateEntryFact",
    "CandidateReason",
    "EvidenceItem",
    "EvidencePack",
    "ExceptionSubject",
    "assemble_evidence",
    "evidence_id_for",
]

#: The namespace every evidence id is derived under. A fixed UUID, so the derivation is reproducible
#: on any machine, and deliberately not the fixture generator's: a corpus id and an evidence id must
#: never be able to collide.
EVIDENCE_NAMESPACE: Final = uuid.UUID("6f2e5c1a-8b47-5a90-9c33-1d7e4f0b2a68")

#: Kinds this increment can assemble, in the order they appear in a pack. Declared here rather than
#: derived from ``EvidenceKind``: the enum names what FR-5 asks for, this names what the system can
#: produce, and the difference is what ADR-050 records.
ASSEMBLED_KINDS: Final = (EvidenceKind.REMITTANCE_REFERENCE, EvidenceKind.CANDIDATE_LEDGER_ENTRY)

#: How far either side of the value date an entry may sit and still be worth showing.
#:
#: Wider than the matcher's window by design — the matcher's window is an eligibility filter, this
#: is a relevance one. Five weeks covers the cross-period cases the taxonomy names, where the whole
#: question is that a movement settled in one period and belongs in another.
EVIDENCE_WINDOW_DAYS: Final = 35

#: How many candidates one pack may carry.
#:
#: A cap rather than a token budget, because the specification sets no budget and inventing one
#: would be inventing a number. It exists because the fan-out is otherwise quadratic in the
#: commonest ambiguity shape: a reviewer showed 30 identical charges producing 30 exceptions of 31
#: items each, 930 rows from one file. The pack states how many were left out, so truncation is
#: never silent.
MAX_CANDIDATES: Final = 5


class CandidateReason(enum.StrEnum):
    """Why the matcher did not take this entry. The most informative field on a candidate.

    ``INSIDE_TOLERANCE_UNMATCHED`` is the one that matters most, and the one the first version of
    this module hid. An entry the matcher was eligible to take and did not take was refused for a
    reason recorded nowhere — an ambiguity, or a contest with another line. Presenting it as an
    exact match without saying so invited two exceptions to be resolved against one entry, which is
    the double-count the matcher's mutual-uniqueness rule exists to prevent.
    """

    INSIDE_TOLERANCE_UNMATCHED = "inside_tolerance_unmatched"
    OUTSIDE_AMOUNT_BAND = "outside_amount_band"
    OUTSIDE_DATE_WINDOW = "outside_date_window"
    OUTSIDE_AMOUNT_BAND_AND_DATE_WINDOW = "outside_amount_band_and_date_window"


@dataclasses.dataclass(frozen=True, slots=True)
class CandidateEntryFact:
    """One unconsumed ledger entry, as persisted. System-owned facts only."""

    entry_id: uuid.UUID
    external_ref: str
    account_code: str
    amount: decimal.Decimal
    currency: str
    booked_on: dt.date
    description: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class ExceptionSubject:
    """The exception being assembled for, and the settlement facts behind it.

    Everything here is persisted and system-owned. Nothing comes from a model, and nothing from
    fixture construction metadata — a scenario label or an intended classification would make the
    pack an answer key rather than evidence, and a guard test asserts this package cannot import the
    fixture package at all.
    """

    exception_id: uuid.UUID
    classification: ExceptionClassification
    settlement_line_id: uuid.UUID
    psp_reference: str
    merchant_reference: str | None
    transaction_type: str | None
    amount: decimal.Decimal
    currency: str
    value_date: dt.date


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One addressable evidence record (FR-5), with the id a proposal may cite.

    ``facts`` is a mapping of named values, never a rendered string. ``None`` means the fact is
    genuinely absent — itself evidence, and why absence is represented rather than omitted.
    """

    evidence_id: uuid.UUID
    kind: EvidenceKind
    facts: Mapping[str, str | None]
    source_ref: str | None


@dataclasses.dataclass(frozen=True, slots=True)
class EvidencePack:
    """The evidence for one exception, and what was left out of it.

    ``omitted_candidates`` is on the pack rather than implied by its length, because silent
    truncation is the failure mode a cap introduces. A reader — human or model — is told that more
    entries were nearby and that the ones shown are the closest.
    """

    items: tuple[EvidenceItem, ...]
    omitted_candidates: int

    def __iter__(self) -> Iterator[EvidenceItem]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> EvidenceItem:
        return self.items[index]


def evidence_id_for(exception_id: uuid.UUID, kind: EvidenceKind, discriminator: str) -> uuid.UUID:
    """The stable id for one evidence item.

    Derived from the exception, the kind and a discriminator that identifies the item *within* that
    kind — never from its content. Content-derived ids would look attractive and would be wrong:
    correcting a typo in a reference would silently become a different piece of evidence, orphaning
    every proposal that cited the old one.

    The exception id is part of the derivation, which is what makes cross-exception collision
    impossible: the same ledger entry offered to two exceptions gets two ids, and neither can be
    mistaken for the other.
    """
    return uuid.uuid5(EVIDENCE_NAMESPACE, f"{exception_id}|{kind.value}|{discriminator}")


def _reason(
    subject: ExceptionSubject, entry: CandidateEntryFact, policy: TolerancePolicy
) -> CandidateReason:
    """Why the matcher did not take this entry, in the matcher's own terms.

    ``policy.band`` and ``policy.within_window`` are the matcher's API, called rather than
    reimplemented. The first version inlined the arithmetic, and a reviewer pointed out that this
    module's claim to be unable to drift from the matcher was then false — the policy *data* was
    shared, the predicate was a copy. Changing the matcher's semantics through its own API now
    changes this too.
    """
    band = policy.band(subject.currency)
    inside_amount = band is not None and abs(entry.amount - subject.amount) <= band
    inside_window = policy.within_window(subject.value_date, entry.booked_on)

    if inside_amount and inside_window:
        return CandidateReason.INSIDE_TOLERANCE_UNMATCHED
    if inside_amount:
        return CandidateReason.OUTSIDE_DATE_WINDOW
    if inside_window:
        return CandidateReason.OUTSIDE_AMOUNT_BAND
    return CandidateReason.OUTSIDE_AMOUNT_BAND_AND_DATE_WINDOW


def _remittance_facts(subject: ExceptionSubject) -> dict[str, str | None]:
    """The references, as named fields.

    Absence is ``None`` rather than a sentinel string. "The merchant sent no reference" is evidence
    — it is why several exceptions in this corpus cannot be tied to anything — and a sentinel a
    merchant could type would let a third party forge that statement.
    """
    return {
        "psp_reference": subject.psp_reference,
        "merchant_reference": subject.merchant_reference,
        "declared_type": subject.transaction_type,
    }


def _candidate_facts(
    entry: CandidateEntryFact, subject: ExceptionSubject, reason: CandidateReason
) -> dict[str, str | None]:
    """One candidate, with its difference from the line and the matcher's verdict on it."""
    return {
        "external_ref": entry.external_ref,
        "account_code": entry.account_code,
        "amount": str(entry.amount),
        "currency": entry.currency,
        "booked_on": entry.booked_on.isoformat(),
        "amount_delta": str(entry.amount - subject.amount),
        "day_delta": str((entry.booked_on - subject.value_date).days),
        "not_matched_because": reason.value,
        "description": entry.description,
    }


def assemble_evidence(
    subject: ExceptionSubject,
    candidates: Iterable[CandidateEntryFact],
    policy: TolerancePolicy,
) -> EvidencePack:
    """The evidence pack for one exception. Pure, ordered, bounded, and the same on every run.

    Ordering is explicit at every level — kinds in ``ASSEMBLED_KINDS`` order, candidates by
    proximity with a total tie-break — so nothing depends on PostgreSQL's row order, a set's
    iteration order, or the order a caller happened to build a list in. Two assemblies of the same
    facts produce the same pack, which is what makes the prompt hash mean anything.
    """
    items: list[EvidenceItem] = [
        EvidenceItem(
            evidence_id=evidence_id_for(
                subject.exception_id,
                EvidenceKind.REMITTANCE_REFERENCE,
                str(subject.settlement_line_id),
            ),
            kind=EvidenceKind.REMITTANCE_REFERENCE,
            facts=_remittance_facts(subject),
            source_ref=f"settlement_line:{subject.settlement_line_id}",
        )
    ]

    nearby = [
        entry
        for entry in candidates
        if entry.currency == subject.currency
        and abs((entry.booked_on - subject.value_date).days) <= EVIDENCE_WINDOW_DAYS
    ]
    ranked = _ranked(nearby, subject)

    for entry in ranked[:MAX_CANDIDATES]:
        items.append(
            EvidenceItem(
                evidence_id=evidence_id_for(
                    subject.exception_id,
                    EvidenceKind.CANDIDATE_LEDGER_ENTRY,
                    str(entry.entry_id),
                ),
                kind=EvidenceKind.CANDIDATE_LEDGER_ENTRY,
                facts=_candidate_facts(entry, subject, _reason(subject, entry, policy)),
                source_ref=f"ledger_entry:{entry.entry_id}",
            )
        )

    return EvidencePack(items=tuple(items), omitted_candidates=max(0, len(ranked) - MAX_CANDIDATES))


def _ranked(
    entries: Sequence[CandidateEntryFact], subject: ExceptionSubject
) -> list[CandidateEntryFact]:
    """Nearest first: amount, then date, then a total tie-break.

    The entry id is the tie-break rather than a sort key, so the pack reads in an order that means
    something while still being total. A total order matters more than the aesthetics: without the
    final id, two entries identical in every other field would order by whatever the input sequence
    happened to do.
    """
    return sorted(
        entries,
        key=lambda entry: (
            abs(entry.amount - subject.amount),
            abs((entry.booked_on - subject.value_date).days),
            entry.external_ref,
            entry.entry_id.hex,
        ),
    )
