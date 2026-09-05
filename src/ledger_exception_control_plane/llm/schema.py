"""The closed response contract (§6.1). The only shape a model may return.

Everything the model is allowed to say is here, and nothing else can be expressed. That is the
whole containment argument, and it is structural rather than procedural: a hallucinated amount is
not caught downstream, it has no field to arrive in.

The schema carries **no numeric type anywhere in its tree** — not an ``int``, not a ``float``, not a
``Decimal``, not a numeric enum value, and no JSON Schema property of type ``number`` or
``integer``. Confidence is the usual place this leaks, so confidence is a closed band. Every model
in the tree sets ``extra="forbid"``, so a provider cannot introduce a field the contract does not
name — an ``amount`` key in a response body is a validation error, not an ignored extra.

``strict=True`` matters more than it looks. This boundary parses text a third party produced: in
lax mode Pydantic would accept ``"true"`` for a bool and ``1`` for an enum, which is exactly how a
malformed provider response becomes a plausible-looking domain object. Strict mode refuses, and the
wire path uses :meth:`model_validate_json` so JSON types are checked as JSON types.

What is deliberately *not* here: any subset check tying ``evidence_refs`` to the evidence actually
supplied. Nothing assembles evidence yet — that is 3.3 — and a check against a set nobody has built
would be a check against an empty set. It belongs at the boundary that owns evidence assembly.
"""

from __future__ import annotations

from typing import Final

from pydantic import BaseModel, ConfigDict, model_validator

from ledger_exception_control_plane.db.control import ConfidenceBand, TreatmentCode

__all__ = ["EvidenceRef", "ProposalPrompt", "TreatmentProposal", "proposal_wire_schema"]

#: Applied to every model in the tree. Closed, strict, and immutable once validated.
#:
#: ``frozen`` is not decoration: a validated proposal is a provenance record of what a model said,
#: and a record that later code can edit is not provenance. It also makes the abstention invariant
#: below an invariant rather than an entry check — the M3.1 account-table lesson, applied before it
#: could be repeated here.
_CLOSED: Final = ConfigDict(extra="forbid", strict=True, frozen=True)

#: Prose Pydantic folds in from docstrings and field names. Removed from the wire copy.
_DOCUMENTATION_KEYS: Final = frozenset({"description", "title"})

#: Keys whose *values* map names to schemas, so their keys must never be filtered.
_NAMED_SCHEMA_MAPS: Final = frozenset({"properties", "$defs", "definitions", "patternProperties"})


class EvidenceRef(BaseModel):
    """A pointer to a record the system already holds (§6.1). It carries no value.

    An opaque string identifier and nothing else, because the alternative — letting the model
    return the evidence *content* it is citing — would let a proposal manufacture a fact by
    asserting it. The system knows what the evidence says; the model may only say which piece of it
    mattered.
    """

    model_config = _CLOSED

    evidence_id: str


class TreatmentProposal(BaseModel):
    """What the model returns for one exception: a categorical choice, and provenance.

    The field list is `PROJECT_SPEC.md` §6.1 exactly. Nothing was added — in particular no account,
    no period, no operation identifier and no amount, which are all system-owned and deterministic
    (§7). A model that wanted to post to account 4900 in period 2026-01 has no way to say so.
    """

    model_config = _CLOSED

    #: The canonical M3.1 vocabulary, imported rather than restated. A second declaration anywhere
    #: in the package fails a guard test, because "closed" is only checkable if there is one set.
    treatment: TreatmentCode

    #: A band, never a score. A number here would be the easiest place to argue an exception —
    #: "it is only a confidence" — and the first numeric field would end the no-numeric claim.
    confidence: ConfidenceBand

    #: Human-readable provenance. Never parsed, never tokenised for numbers, never branched on, and
    #: structurally unable to reach the calculator (§6.2). Unbounded, because the specification
    #: sets no maximum and the column behind it is ``Text``; inventing a limit here would silently
    #: truncate or reject provenance the system was told to keep.
    rationale: str

    #: A tuple, not a list, and that was a correction. ``frozen=True`` blocks attribute
    #: *assignment*; it does nothing about the container an attribute holds, so a validated
    #: proposal could have its citations appended to or cleared in place — by anything holding a
    #: reference to it, after validation, after the citation check. A reviewer emptied one.
    #:
    #: The citation check is the reason this matters rather than being a tidiness point. It refuses
    #: a proposal whole rather than trimming it, precisely so nobody rewrites a provenance record;
    #: a mutable list let a caller do afterwards exactly what the check exists to forbid. Third
    #: occurrence of this shape in the project, after ``AccountPolicy`` and ``ProviderRequest``.
    evidence_refs: tuple[EvidenceRef, ...]

    #: Not a fifth treatment. A model that declines to answer has still not chosen an action, so
    #: abstention is a separate flag — and one that must coincide with escalation, below.
    abstained: bool

    @model_validator(mode="after")
    def _abstention_escalates(self) -> TreatmentProposal:
        """An abstaining proposal must carry ``ESCALATE``.

        The implication runs one way, matching the database constraint this contract has to agree
        with (``NOT abstained OR treatment = 'escalate'``) and ADR-048. Abstaining while proposing
        ``REBOOK`` is a contradiction — the model both declined to decide and recommended an
        action — and is rejected rather than normalised, because silently clearing one of the two
        fields would decide on the model's behalf which half it meant.

        Escalating *without* abstaining stays valid, and deliberately so: a model that has read the
        evidence and concluded a human must look at this has made a real decision, and it is not
        the same event as declining to answer. Collapsing the two would lose the distinction the
        audit trail exists to keep.
        """
        if self.abstained and self.treatment is not TreatmentCode.ESCALATE:
            raise ValueError(
                f"an abstaining proposal must escalate, not {self.treatment.value!r}",
            )
        return self


class ProposalPrompt(BaseModel):
    """What a caller hands a provider. System-owned text, assembled elsewhere.

    Deliberately thin. Building this out of evidence — deciding what the model is shown, in what
    order, with which identifiers — is 3.3's job, and doing it here would put prompt construction
    behind the port instead of in front of it. The port needs to know only that a prompt is two
    strings, so that two adapters can shape them differently.
    """

    model_config = _CLOSED

    system: str
    user: str


def proposal_wire_schema() -> dict[str, object]:
    """The contract as a provider receives it: the same shape, without the prose.

    Pydantic folds every docstring in the tree into ``description`` keys, and the docstrings here
    are engineering notes — why the set closes, which reviewer broke what, a worked example of the
    numeric escape hatch this project refuses. Three problems with shipping that to a vendor on
    every call: it is hundreds of tokens of nothing the model needs, it hands a third party the
    internal reasoning behind a financial control, and the guard tests that scan for forbidden
    words would be scanning our own commentary about them.

    So the wire copy carries structure only. What the model should *do* is the prompt's job (3.3),
    which is the right place for it — instructions belong in instructions, not smuggled into a
    schema as field documentation.

    Structure is untouched: same properties, same enums, same ``additionalProperties: false``, same
    ``required``. The guards below check both this and the annotated original, because a numeric
    field could not be introduced in one and absent from the other.
    """

    def strip(node: object, *, is_schema: bool) -> object:
        if isinstance(node, dict):
            if not is_schema:
                # A mapping of *names* to schemas — ``properties`` or ``$defs``. Its keys are field
                # names, not keywords, and filtering them here is how a reviewer got a field
                # literally named ``description`` deleted from ``properties`` while it stayed in
                # ``required``: an unsatisfiable schema no response could ever match.
                return {key: strip(value, is_schema=True) for key, value in node.items()}
            return {
                key: strip(value, is_schema=key not in _NAMED_SCHEMA_MAPS)
                for key, value in node.items()
                if key not in _DOCUMENTATION_KEYS
            }
        if isinstance(node, list):
            return [strip(item, is_schema=True) for item in node]
        return node

    stripped = strip(TreatmentProposal.model_json_schema(), is_schema=True)
    assert isinstance(stripped, dict)
    return stripped
