"""Builds the cassette this repository commits, and checks it has not drifted (increment 3.4).

This module exists because of a question a reviewer asked about the first version: *who regenerates
the file?* The builder lived inside the test that drift-checked it, and the failure message told a
reader to run ``make cassettes``, a target that did not exist. A cassette nobody can regenerate is a
binary blob with a JSON extension, and a test that tells you to run a command which is not there is
worse than one that says nothing.

So the builder is a module with a command line, drift-checked the same way the fixture corpus and
the M2 demo snapshot are:

    make cassettes          # rewrite tests/cassettes/canonical-corpus.json
    make cassettes-check    # fail if it has drifted from this code

**It lives under ``tests/`` rather than in the package, and that was not a filing preference.** The
first version sat in ``llm/`` and the M3.3 fixture-truth firewall failed the build immediately: no
module in the package may import the fixture corpus, because a corpus knows the answer to every
case it contains and an assembler able to read a construction label would be showing the model an
answer key. The builder has to run the generator to produce the requests, so it belongs on the side
of that fence where test artifacts are made. The alternative was adding it to the firewall's
allowlist, which would have meant weakening a guard to accommodate a file — the wrong way round.

**What it produces is synthesised, and the file says so.** No credential is present in this
repository, the harness ships no HTTP client, and nothing here has ever spoken to a provider. What
the cassette exercises is real — the adapters' own parsing, the request fingerprint that decides a
match, scrubbing, canonical serialisation, determinism — but it is not evidence about how any model
behaves. Capturing that needs a key and the explicit opt-in
:data:`~.cassette.CAPTURE_OPT_IN` gates, and belongs to the evaluation increments (6.1 to 6.3).
:class:`~.cassette.Origin` keeps the two apart so the difference can never be quietly lost.

**The answers carry no ground truth.** Treatments are assigned by sorted position, which is an
arbitrary ordering over UUIDs. Deriving them from the fixture generator's intended classification
would be worse than useless: it would bake the answer key into the artifact a later evaluation is
supposed to measure *against*, and the resulting score would measure nothing.

Position rather than a hash of the id, and that was a correction. Hashing looked more principled and
left the abstention branch dead — the committed file exercised neither an abstaining proposal nor
every treatment, so a reviewer's mutants in that region survived. The assignment below covers all
four treatments and produces abstentions, by construction, and a test asserts the coverage rather
than trusting this comment.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Sequence
from typing import Any, Final

from ledger_exception_control_plane.classification import (
    SettlementMovement,
    classify,
    movement_type,
)
from ledger_exception_control_plane.db.control import ConfidenceBand, TreatmentCode
from ledger_exception_control_plane.fixtures.generator import generate
from ledger_exception_control_plane.fixtures.schema import Profile
from ledger_exception_control_plane.llm.cassette import (
    Interaction,
    Origin,
    canonical,
    cassette_id_for,
    render_cassette,
    request_fingerprint,
    scrub,
)
from ledger_exception_control_plane.llm.evidence import (
    CandidateEntryFact,
    ExceptionSubject,
    assemble_evidence,
)
from ledger_exception_control_plane.llm.port import ProviderId, ProviderRequest
from ledger_exception_control_plane.llm.prompt import build_prompt
from ledger_exception_control_plane.llm.providers import (
    ANTHROPIC_MODEL_ID,
    OPENAI_MODEL_ID,
)
from ledger_exception_control_plane.llm.providers.anthropic_messages import (
    AnthropicMessagesProposer,
)
from ledger_exception_control_plane.llm.providers.openai_chat import OpenAIChatProposer
from ledger_exception_control_plane.matching import (
    DEFAULT_POLICY,
    CandidateEntry,
    CandidateLine,
    match,
)

__all__ = [
    "CANONICAL_CASSETTE",
    "CORPUS_SEED",
    "PROVIDERS",
    "build_interactions",
    "corpus_subjects",
    "envelope",
    "render",
    "stand_in_answer",
]

#: Where the committed cassette lives. Absolute, derived from this file, so ``make cassettes``
#: rewrites the file the suite reads no matter which directory it was invoked from.
CANONICAL_CASSETTE: Final = (
    pathlib.Path(__file__).resolve().parent / "cassettes" / ("canonical-corpus.json")
)

#: The seed the committed corpus is generated with — the same arbitrary fixed constant M1.3 uses.
CORPUS_SEED: Final = 20260829

#: How many settlement lines the corpus is built from. Fixed, because the cassette is drift-checked.
CORPUS_LINES: Final = 200

PROVIDERS: Final = (
    (ProviderId.ANTHROPIC, AnthropicMessagesProposer),
    (ProviderId.OPENAI, OpenAIChatProposer),
)


class _UnusableTransport:
    """Satisfies a constructor and nothing else. Building a request never sends one."""

    async def send(self, request: ProviderRequest) -> dict[str, Any]:
        raise AssertionError("the cassette builder never sends")


def corpus_subjects() -> list[tuple[ExceptionSubject, list[CandidateEntryFact]]]:
    """Every exception the canonical corpus produces, with the entries that could explain it.

    The real pipeline — generate, match, classify — run in memory. **Nothing here reads fixture
    construction metadata.** The scenario labels and intended classifications never leave the
    generator, which is the same firewall M2.3 and M3.3 assert: an artifact built from the answer
    key cannot be used to measure anything.

    Ordered by exception id so the output is stable across runs and platforms.
    """
    corpus = generate(CORPUS_SEED, Profile.CANONICAL, CORPUS_LINES)
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

    subjects: list[tuple[ExceptionSubject, list[CandidateEntryFact]]] = []
    for decision in classify([m for m in movements if not m.matched], movements):
        movement = by_id[decision.line_id]
        row = rows[decision.line_id]
        subjects.append(
            (
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
        )
    return sorted(subjects, key=lambda pair: str(pair[0].exception_id))


def stand_in_answer(position: int, evidence_ids: Sequence[str]) -> dict[str, Any]:
    """A valid proposal for the exception at ``position``. Not a model's opinion.

    Assigned round-robin so the committed cassette exercises every treatment, and so that the
    abstention branch — which must coincide with ``ESCALATE``, per the contract and the database
    constraint behind it — is actually reached. The rationale says what the thing is, in the
    artifact itself, where somebody reading the file will see it.
    """
    treatments = list(TreatmentCode)
    treatment = treatments[position % len(treatments)]
    abstained = treatment is TreatmentCode.ESCALATE and position % (2 * len(treatments)) == 3
    return {
        "treatment": treatment.value,
        "confidence": list(ConfidenceBand)[position % len(ConfidenceBand)].value,
        "rationale": "Synthesised stand-in answer. Not produced by a model.",
        "evidence_refs": [{"evidence_id": eid} for eid in evidence_ids[:1]],
        "abstained": abstained,
    }


def envelope(provider: ProviderId, answer: dict[str, Any]) -> dict[str, Any]:
    """The provider's own response shape, including the identifiers scrubbing must remove.

    The identifiers are deliberately present *before* scrubbing. A synthetic payload that never
    carried one would let the scrub step pass while doing nothing.

    **No ``usage`` block, deliberately.** Both vendors return token counts and 6.3 computes cost
    from those fields rather than estimating it — so a synthesised recording carrying
    ``{"input_tokens": 0}`` would read as "this call was free" rather than "nobody measured this
    call", and a fabricated zero would end up inside a published cost figure. Absent is the honest
    encoding, and a test asserts it rather than this comment asking nicely.
    """
    body = canonical(answer)
    if provider is ProviderId.ANTHROPIC:
        return {
            "id": "msg_synthesised_identifier",
            "type": "message",
            "role": "assistant",
            "model": ANTHROPIC_MODEL_ID,
            "content": [{"type": "text", "text": body}],
            "stop_reason": "end_turn",
        }
    return {
        "id": "chatcmpl_synthesised_identifier",
        "object": "chat.completion",
        "system_fingerprint": "fp_synthesised",
        "model": OPENAI_MODEL_ID,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": body, "refusal": None},
                "finish_reason": "stop",
            }
        ],
    }


def build_interactions() -> list[Interaction]:
    """The committed cassette, rebuilt from scratch: every exception, on both providers."""
    interactions: list[Interaction] = []
    for position, (subject, candidates) in enumerate(corpus_subjects()):
        pack = assemble_evidence(subject, candidates, DEFAULT_POLICY)
        prompt = build_prompt(subject, pack)
        answer = stand_in_answer(position, [str(item.evidence_id) for item in pack])

        for provider, adapter in PROVIDERS:
            proposer = adapter(_UnusableTransport())
            request = proposer.build_request(prompt)
            fingerprint = request_fingerprint(request)
            interactions.append(
                Interaction(
                    cassette_id=cassette_id_for(provider, proposer.model_id, fingerprint),
                    provider=provider,
                    model_id=proposer.model_id,
                    model_version=proposer.model_version,
                    path=request.path,
                    request_fingerprint=fingerprint,
                    # Never CAPTURED. Only a recording transport, holding something that could have
                    # spoken to a provider, is allowed to claim that.
                    origin=Origin.SYNTHESISED,
                    response=scrub(envelope(provider, answer)),
                )
            )
    return interactions


def render() -> str:
    """The exact bytes the committed file should contain."""
    return render_cassette(build_interactions())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, prog="cassette-corpus")
    parser.add_argument("command", choices=("generate", "verify"))
    parser.add_argument("--out", type=pathlib.Path, default=CANONICAL_CASSETTE)
    args = parser.parse_args(argv)

    expected = render()
    path: pathlib.Path = args.out

    if args.command == "generate":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8", newline="\n")
        print(f"wrote {path} ({len(expected)} bytes)")
        return 0

    if not path.is_file():
        print(f"{path} is missing; run `make cassettes`", file=sys.stderr)
        return 1
    if path.read_text(encoding="utf-8") != expected:
        print(f"{path} has drifted from the builder; run `make cassettes`", file=sys.stderr)
        return 1
    print(f"{path} matches the builder")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the Makefile
    raise SystemExit(main())
