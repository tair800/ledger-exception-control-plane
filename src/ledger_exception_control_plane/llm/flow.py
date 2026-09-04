"""The proposal flow: ask, validate, and say plainly what happened (increment 3.3).

Four outcomes, and the reason they are four rather than "a proposal or an exception" is the whole
point of this module. A caller has to be able to tell *the model declined to judge this case* from
*the provider was unreachable* from *the provider answered nonsense* — those want three different
responses from an operator, and collapsing any two of them would put a decision in the audit trail
that nobody made.

**No outcome is ever a guess.** There is no path here from an unreachable provider, or from
unparseable text, to ``REBOOK``, ``ACCRUE`` or ``WRITE_OFF``. There is not even a path to
``ESCALATE``: escalation is something a model chooses inside the contract, so manufacturing one
would be recording a model decision that no model made. When there is no valid answer the outcome
carries no proposal at all, and the exception stays exactly as it was — open, unproposed, waiting
for a human. That is what NFR-11's "queue for human treatment" means in a system where the queue is
the set of exceptions without a resolution, and it is why this module never writes to the
deterministic path.

**Evidence citations are checked against what was actually shown.** M3.2 left this open on purpose,
because a subset check needs a set and nothing assembled evidence then. It closes here: a proposal
may cite only ids that were in the pack sent with that request. An unknown id is not dropped and not
rewritten — a citation nobody can resolve is a fabricated provenance record, and silently deleting
it would leave a proposal whose rationale refers to evidence the audit trail cannot show.
"""

from __future__ import annotations

import dataclasses
import enum
from typing import Final

from ledger_exception_control_plane.db.control import TreatmentCode
from ledger_exception_control_plane.llm.evidence import EvidencePack, ExceptionSubject
from ledger_exception_control_plane.llm.port import (
    ProviderResponseError,
    ProviderUnavailableError,
    TreatmentProposer,
)
from ledger_exception_control_plane.llm.prompt import build_prompt, prompt_hash
from ledger_exception_control_plane.llm.schema import TreatmentProposal

__all__ = [
    "CitationError",
    "ProposalOutcome",
    "ProposalStatus",
    "assert_citations_were_supplied",
    "propose_treatment",
]


class ProposalStatus(enum.StrEnum):
    """What happened. Closed, so a caller can exhaust it."""

    #: A valid proposal, citing only supplied evidence. May still be an abstention.
    PROPOSED = "proposed"

    #: The provider could not be reached, or failed before answering. Nothing was recorded.
    UNAVAILABLE = "unavailable"

    #: The provider answered, and the answer is not a usable proposal — malformed, outside the
    #: closed vocabulary, or citing evidence it was never shown.
    INVALID = "invalid"


class CitationError(ProviderResponseError):
    """A proposal cited evidence that was not supplied to it.

    A subclass of the M3.2 response error rather than a new kind of failure: from the caller's point
    of view the provider answered and the answer is unusable, which is exactly what that type
    means. The distinct class exists so the reason survives into the audit trail.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """The result of asking one model about one exception.

    ``proposal`` is present only for :data:`ProposalStatus.PROPOSED` — enforced below, not merely
    documented — and ``detail`` only for the two failures. The prompt hash is here for
    every outcome, failures included: knowing exactly what was asked is most valuable
    precisely when the answer was unusable.
    """

    status: ProposalStatus
    prompt_hash: str
    proposal: TreatmentProposal | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """The pairing was prose until a reviewer pointed out that prose is not an invariant.

        An outcome carrying both a failure status and a proposal would report a treatment for a
        call that produced none — and every argument in this module is expressed in this type, so
        it is the one place the pairing has to be structural rather than described.
        """
        proposed = self.status is ProposalStatus.PROPOSED
        if proposed and self.proposal is None:
            raise ValueError("a proposed outcome must carry its proposal")
        if not proposed and self.proposal is not None:
            raise ValueError(f"a {self.status.value} outcome must carry no proposal")

    @property
    def abstained(self) -> bool:
        """Whether the model declined to judge the case.

        Reads off the proposal rather than being tracked separately, so it cannot disagree with the
        stored record. A failure is not an abstention and never reports as one.
        """
        return self.proposal is not None and self.proposal.abstained

    @property
    def treatment(self) -> TreatmentCode | None:
        """The only value that may ever reach the money path, and only when it is real."""
        return self.proposal.treatment if self.proposal is not None else None


def assert_citations_were_supplied(proposal: TreatmentProposal, evidence: EvidencePack) -> None:
    """Every cited id must be one that was in the pack. Duplicates are refused too.

    Duplicates are not obviously wrong — a model citing the same item twice has not lied about
    anything — but the association table this becomes has a composite primary key over
    ``(proposal, evidence)``, so a duplicate is a row that cannot be written. Refusing it here
    reports the real reason rather than surfacing an integrity error from three layers down, and it
    keeps the citation list a set, which is what "the evidence this proposal relied on" means.

    **A duplicate is a duplicate identifier, not a duplicate string.** ``uuid.UUID`` accepts upper
    case, braces, the URN form and no hyphens at all, so one id has at least five spellings and a
    model producing two of them is producing ordinary output.
    """
    supplied = {item.evidence_id for item in evidence}
    cited = [(ref.evidence_id, _as_uuid(ref.evidence_id)) for ref in proposal.evidence_refs]

    unknown = sorted({spelling for spelling, parsed in cited if parsed not in supplied})
    if unknown:
        raise CitationError(f"the proposal cites evidence that was not supplied: {unknown}")

    # Compared on the *parsed* identifier, never on the spelling. Three reviewers found the same
    # defect here independently: the check above parsed, this one compared strings, so
    # `c2e1aafc-…` and `C2E1AAFC-…` passed as two distinct citations and then collided on the
    # association table's composite primary key — a raw `IntegrityError` escaping the one function
    # whose documented contract is that bad model output yields one of three outcomes.
    identifiers = [parsed for _spelling, parsed in cited]
    duplicates = sorted({spelling for spelling, parsed in cited if identifiers.count(parsed) > 1})
    if duplicates:
        raise CitationError(f"the proposal cites the same evidence more than once: {duplicates}")


def _as_uuid(reference: str) -> object:
    """Compare citations to supplied ids as UUIDs, not as text.

    The contract types an ``evidence_id`` as an opaque string, and two spellings of one UUID —
    upper case, braces, urn form — are the same identifier. Comparing the strings would let a
    correct citation be rejected for its punctuation; parsing means an unparseable citation simply
    matches nothing and is reported as unknown, which is what it is.
    """
    import uuid

    try:
        return uuid.UUID(reference)
    except ValueError:
        return reference


_UNAVAILABLE_DETAIL: Final = "provider unavailable; the exception is left for human treatment"


async def propose_treatment(
    proposer: TreatmentProposer,
    subject: ExceptionSubject,
    evidence: EvidencePack,
) -> ProposalOutcome:
    """Ask one provider about one exception, and report what came back.

    Pure with respect to the database: this function reads nothing and writes nothing. Persistence
    is the service layer's job, and keeping the two apart is what lets every branch here be tested
    without a database and without a network.
    """
    prompt = build_prompt(subject, evidence)
    digest = prompt_hash(prompt)

    try:
        proposal = await proposer.propose(prompt)
    except ProviderUnavailableError as exc:
        return ProposalOutcome(
            status=ProposalStatus.UNAVAILABLE,
            prompt_hash=digest,
            detail=f"{_UNAVAILABLE_DETAIL}: {exc}",
        )
    except ProviderResponseError as exc:
        return ProposalOutcome(status=ProposalStatus.INVALID, prompt_hash=digest, detail=str(exc))

    try:
        assert_citations_were_supplied(proposal, evidence)
    except CitationError as exc:
        return ProposalOutcome(status=ProposalStatus.INVALID, prompt_hash=digest, detail=str(exc))

    return ProposalOutcome(status=ProposalStatus.PROPOSED, prompt_hash=digest, proposal=proposal)
