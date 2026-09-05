"""The retry-independent operation identifier (increment 4.1).

`PROJECT_SPEC.md` §12.1 and ADR-004b fix the derivation, and this module implements it rather than
re-deciding it::

    operation_id = SHA256( DOMAIN_TAG
                         || len_prefixed(exception_id)
                         || len_prefixed(resolution_version)
                         || len_prefixed(instruction_payload_hash) )

**The identifier is derived independently of retries, and that is the whole point.** Attempt one and
attempt five of the same approved resolution produce the identical value. Nothing here reads a
clock, a counter, a hostname, a process id or a random source — and a test walks this module's own
syntax tree to prove it, because the failure is silent: a system whose identifier varies with the
attempt looks entirely correct until the first retry, at which point provider-side suppression and
reconciliation-by-query both quietly stop working.

**What the payload binds, and why more than the specification enumerates.** §12.1 names six things
that determine the financial effect — treatment, amount, currency, account, period and the
ledger-context version. This module binds every field of :class:`~..money.AdjustmentInstruction`,
which is those six plus ``exception_id``, ``quantum`` and ``rounding``. That is deliberate and
strictly stronger: the increment's own test obligation is that *mutating any component of the
posting instruction changes the identifier*, and a quantisation or rounding change is a change in
how the amount was determined. The binding is checked against the dataclass itself, so a field added
to the instruction later cannot silently escape the hash.

**What is deliberately excluded: the approver.** §16 permits the approver to differ for the same
economic event — a re-approval, or an edit requiring a different principal — so an identifier that
varied with them would vary with a non-financial input. That is the mirror image of the
retry-dependence this module exists to prevent, and it fails just as silently. The approver is
recorded in ``approval`` and in the audit trail, never in the key.

**Nothing here claims anything about the ledger.** §12.3 is explicit: sending an operation
identifier to an external system does not make anything idempotent — it is a *request* for
idempotent treatment, honoured only if the downstream ledger implements one. This module supplies a
stable key. Whether a duplicate is suppressed is a property of the adapter (4.2) and conditional on
its declared capability (§13.5).
"""

from __future__ import annotations

import dataclasses
import decimal
import hashlib
import uuid
from typing import Final

from ledger_exception_control_plane.db.base import (
    MONEY_MAGNITUDE_EXCLUSIVE_BOUND,
    MONEY_MAX_SCALE,
    within_money_scale,
)
from ledger_exception_control_plane.money import AdjustmentInstruction

__all__ = [
    "INSTRUCTION_DOMAIN_TAG",
    "OPERATION_DOMAIN_TAG",
    "PAYLOAD_COMPONENTS",
    "AmountNotStorableError",
    "OperationIdentity",
    "canonical_amount",
    "derive_identity",
    "instruction_payload_hash",
    "operation_id",
]

#: Domain-separation tag for the operation identifier. Versioned, because a change to what the
#: identifier means is a change every stored identifier has to be readable against.
#:
#: Two distinct tags, not one, so a payload digest can never be mistaken for an operation
#: identifier: both are 64 hex characters and both satisfy the same column check, so without
#: separation a mix-up would be storable.
OPERATION_DOMAIN_TAG: Final = b"lecp.operation.v1"

#: Domain-separation tag for the instruction payload digest.
INSTRUCTION_DOMAIN_TAG: Final = b"lecp.instruction.v1"

#: Every field of the instruction, in the order they are hashed.
#:
#: Declared rather than derived from ``dataclasses.fields`` at hash time, so the hashed order is
#: fixed by this file and cannot be silently reordered by an edit to the dataclass — reordering
#: would change every identifier ever derived. A test asserts this tuple covers the dataclass
#: exactly, so a *new* field cannot escape the hash either.
PAYLOAD_COMPONENTS: Final = (
    "exception_id",
    "treatment",
    "amount",
    "currency",
    "account_code",
    "period",
    "quantum",
    "rounding",
    "ledger_context_version",
)


class AmountNotStorableError(ValueError):
    """The amount cannot be encoded exactly at the money contract's scale.

    Raised rather than rounded. An amount the ``adjustment`` column would refuse must not acquire an
    operation identifier: the identifier would name a posting that can never be stored, and rounding
    it here would be the database-rounds-on-the-way-in failure that M1.1 spent its time removing,
    relocated one layer up.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class OperationIdentity:
    """The two digests §12.1 derives, together, because they are only meaningful as a pair.

    ``adjustment`` stores both. Returning them separately invites a caller to persist one from this
    derivation and the other from somewhere else, and the pair would then no longer be evidence of
    anything.
    """

    operation_id: str
    instruction_payload_hash: str


def canonical_amount(amount: decimal.Decimal) -> str:
    """One economic value, one textual form — computed exactly, with no reference to any context.

    **This function is the reason the identifier is stable at all.** ``Decimal("120.45")`` and
    ``Decimal("120.450000")`` are the same amount and compare equal, but they are different objects
    with different string forms, so hashing either directly would make the identifier depend on how
    a value happened to be spelled. ``db/base.py`` already names this hazard and solves half of it
    by quantising on read; a value that never went through a read would otherwise defeat the other
    half.

    **Not ``quantize``, and that is not a style choice.** ``quantize`` is a context operation: it
    raises when the result exceeds ``decimal.getcontext().prec`` and rounds according to the ambient
    rounding mode. Both make the answer depend on what some unrelated caller set the context to,
    which is exactly the defect ``within_money_scale`` was rewritten to remove after an amount with
    29 decimal places scaled to something integral and was accepted. Everything below is integer
    arithmetic on the digits ``as_tuple`` reports, so nothing is rounded and nothing is contextual.

    Non-finite and over-precise values raise rather than being coerced, and every spelling of
    zero — ``0``, ``-0``, ``0.0000``, ``0E+400000`` — canonicalises to ``0.0000``. One value, one
    spelling, and the sign that ``Decimal`` keeps on a negative zero is dropped with it.
    """
    if not amount.is_finite():
        raise AmountNotStorableError(f"an amount must be finite, not {amount}")
    if not within_money_scale(amount):
        raise AmountNotStorableError(
            f"{amount} needs more than {MONEY_MAX_SCALE} decimal places, so the adjustment column "
            "would refuse it; it must not acquire an operation identifier"
        )
    if amount.copy_abs() >= MONEY_MAGNITUDE_EXCLUSIVE_BOUND:
        raise AmountNotStorableError(
            f"{amount} is at or beyond the money magnitude bound {MONEY_MAGNITUDE_EXCLUSIVE_BOUND}"
        )

    sign, digits, exponent = amount.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - `is_finite` already excluded these
        # A special value's exponent is a marker string rather than a number. Unreachable, and
        # refused rather than asserted away: an exception carries its reason to a caller, and an
        # assertion vanishes under `python -O`.
        raise AmountNotStorableError(f"{amount} has no numeric exponent")

    unscaled = 0
    for digit in digits:
        unscaled = unscaled * 10 + digit

    if not unscaled:
        # Zero at any exponent, short-circuited before the shift below — and this is a fix, not an
        # optimisation. `Decimal` admits an arbitrary exponent, and both the scale and magnitude
        # checks above pass for every spelling of zero: `0E+400000` is exact at four places and is
        # comfortably under the magnitude bound. The shift would then materialise `10**400004`,
        # which a reviewer measured at 42 ms for a ten-character input and which grows without
        # limit. Zero is the only value that can reach a large shift — a large positive exponent
        # with a non-zero digit fails the magnitude check, and a large negative one fails the scale
        # check — so handling it here bounds the whole function.
        return f"0.{'0' * MONEY_MAX_SCALE}"

    # `within_money_scale` guarantees the value is exact at four places, so shifting to integer
    # units of the quantum is exact in both directions: multiply when the exponent is coarser than
    # the quantum, divide when it is finer and the extra digits are provably zero.
    shift = exponent + MONEY_MAX_SCALE
    units = unscaled * 10**shift if shift >= 0 else unscaled // 10**-shift

    whole, fraction = divmod(units, 10**MONEY_MAX_SCALE)
    # `sign` is dropped when the value is zero, so `-0.0000` and `0.0000` produce one string.
    marker = "-" if sign else ""
    return f"{marker}{whole}.{fraction:0{MONEY_MAX_SCALE}d}"


def _framed(value: bytes) -> bytes:
    """One length-prefixed component.

    §12.1 bans unprefixed concatenation by name, because it is a live collision source: without a
    prefix, the components ``("ab", "c")`` and ``("a", "bc")`` hash identically, and two different
    postings sharing one identifier is the precise failure the identifier exists to prevent. Eight
    bytes, big-endian, so the width cannot depend on the value.
    """
    return len(value).to_bytes(8, "big") + value


def _labelled(label: str, value: str) -> bytes:
    """A named component, framed on both halves.

    Stronger than §12.1 requires, and only for the payload digest, whose composition the
    specification states as *content* rather than as a formula. Labelling means two fields cannot be
    swapped without changing the digest even if their values are interchangeable — ``currency`` and
    ``account_code`` both being short upper-case strings, for instance.
    """
    return _framed(label.encode("utf-8")) + _framed(value.encode("utf-8"))


def _canonical_decimal(value: decimal.Decimal) -> str:
    """One spelling per value, for a decimal that is not a monetary amount.

    ``quantum`` is a ``Decimal`` and it is **not** an amount, which the first version missed: it
    dispatched on type, so the declared quantisation was judged by the ``adjustment`` column's
    four-place rule. Today ``MONEY_QUANTUM`` is ``0.0001`` and nothing failed; an instruction that
    ever recorded a finer, deliberately-audited quantisation would have been refused with a message
    about a column it has nothing to do with. A reviewer read the coupling before it could bite.

    The encoding strips trailing zeros by integer arithmetic rather than by ``normalize()``, which
    is a context operation — the same reason ``canonical_amount`` reads digits instead of
    quantising.
    """
    if not value.is_finite():
        raise AmountNotStorableError(f"{value} has no canonical decimal form")

    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - `is_finite` already excluded these
        raise AmountNotStorableError(f"{value} has no numeric exponent")

    unscaled = 0
    for digit in digits:
        unscaled = unscaled * 10 + digit
    if not unscaled:
        return "0E0"
    while unscaled % 10 == 0:
        unscaled //= 10
        exponent += 1
    return f"{'-' if sign else ''}{unscaled}E{exponent}"


def _component(instruction: AdjustmentInstruction, name: str) -> str:
    """The canonical text of one instruction field.

    Dispatched on the **field**, not on its type, and that was a correction. Type dispatch put the
    declared quantisation through the monetary amount's storability rule simply because both are
    ``Decimal``.

    Every branch is exact and total. There is deliberately no ``str(value)`` fallback: a field this
    function does not recognise must fail loudly at the moment it is added, rather than acquire a
    representation-dependent encoding that silently destabilises every identifier derived
    afterwards.
    """
    value = getattr(instruction, name)
    if name == "amount":
        if not isinstance(value, decimal.Decimal):
            # Never `assert`: the money contract is the one place in this project where a check
            # disappearing under `python -O` would be worst, and `Decimal` is not something an
            # amount may merely resemble.
            raise TypeError(f"amount is {type(value).__name__}, not a Decimal")
        return canonical_amount(value)
    if isinstance(value, decimal.Decimal):
        return _canonical_decimal(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, str):  # includes `TreatmentCode`, a `StrEnum`
        return str(value)
    raise TypeError(
        f"{name} is {type(value).__name__}, which has no canonical encoding here; add one "
        "deliberately rather than letting str() decide"
    )


def instruction_payload_hash(instruction: AdjustmentInstruction) -> str:
    """Everything that determines the financial effect, in one digest (§12.1).

    If account mapping or period configuration changes between a first attempt and a re-send, the
    instruction is genuinely different and this digest — and therefore the operation identifier —
    **must** differ. Binding only the treatment code is named in the specification as a defect, with
    the failure spelled out: under ``ENFORCES_KEY`` the provider would suppress the second posting
    while this system recorded ``CONFIRMED`` for something that was never applied.
    """
    body = b"".join(_labelled(name, _component(instruction, name)) for name in PAYLOAD_COMPONENTS)
    return hashlib.sha256(INSTRUCTION_DOMAIN_TAG + body).hexdigest()


def operation_id(
    *, exception_id: uuid.UUID, resolution_version: int, instruction_payload_hash: str
) -> str:
    """The §12.1 formula, literally: a domain tag then three length-prefixed components.

    ``resolution_version`` is here because a corrected resolution is a *different* operation rather
    than a silent overwrite of the previous one. The approver is not here; see the module docstring.
    """
    if resolution_version < 1:
        raise ValueError(f"a resolution version starts at 1, not {resolution_version}")
    if not _is_sha256_hex(instruction_payload_hash):
        raise ValueError(
            "the instruction payload hash must be a 64-character lower-case SHA-256 digest"
        )

    body = b"".join(
        _framed(part.encode("utf-8"))
        for part in (str(exception_id), str(resolution_version), instruction_payload_hash)
    )
    return hashlib.sha256(OPERATION_DOMAIN_TAG + body).hexdigest()


def derive_identity(
    instruction: AdjustmentInstruction, *, exception_id: uuid.UUID, resolution_version: int
) -> OperationIdentity:
    """Both digests for one approved resolution.

    ``exception_id`` is passed separately and checked against the instruction rather than simply
    read off it. The two come from different places — the approval that authorised the write, and
    the calculation that priced it — and an instruction priced for one exception must never be
    identified under another exception's approval. That would post exception A's amount against
    exception B's authorisation, and no database constraint catches it: ``adjustment`` has no
    ``exception_id`` column, reaching the exception only through the approval.
    """
    if instruction.exception_id != exception_id:
        raise ValueError(
            f"the instruction prices exception {instruction.exception_id} but is being identified "
            f"under exception {exception_id}"
        )

    payload = instruction_payload_hash(instruction)
    return OperationIdentity(
        operation_id=operation_id(
            exception_id=exception_id,
            resolution_version=resolution_version,
            instruction_payload_hash=payload,
        ),
        instruction_payload_hash=payload,
    )


def _is_sha256_hex(value: str) -> bool:
    """The same shape the ``adjustment`` column's check constraint enforces."""
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
