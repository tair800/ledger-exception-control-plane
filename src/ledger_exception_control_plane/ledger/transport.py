"""Classifying a transport failure that happened *instead of* an answer (increment 4.3).

`PROJECT_SPEC.md` §15 is unusually specific here, and the specificity is the mechanism:

    Retries apply only to transport failures on an **enumerated, testable allowlist** where no byte
    of the request was written: **DNS resolution failure · TCP connect failure or refusal · TLS
    handshake failure · connect-timeout before first byte written.** … **The default is `UNKNOWN`,
    not retryable:** any transport error not on the allowlist is `UNKNOWN`. The word "provably" is
    deliberately avoided, because nothing at the client proves non-arrival — a gateway can accept,
    forward, and then fail the client leg. **The classifier *is* the guarantee, so it is enumerated
    rather than described.**

That last sentence is why this module exists as a closed enumeration rather than as a few
``except`` clauses in the dispatcher, and why the default arm is the *unsafe-to-retry* one.

**Why this lives in `ledger/` and not in `operations/`.** The three unambiguous causes are named by
exception types from :mod:`socket` and :mod:`ssl`, and a guard test forbids the whole ``operations``
package from importing either — it must not be able to open a socket. Classification of a ledger
transport failure is a property of the ledger boundary, so it belongs on this side of it. Putting it
in ``operations`` would have meant widening that fence, which is a relaxation; putting it here is
not.

**Nothing here opens a socket either.** It reads an exception object that some future real adapter
raised and answers a question about it. The reference adapter is in-process and raises nothing of
the kind; the classifier is exercised by handing it the exceptions directly, which is what makes it
testable rather than described.

**A declared cause outranks a guessed one, and an undeclared cause is never retryable.** Two of the
four causes cannot be recognised from a standard exception type at all:

- A ``TimeoutError`` may be a *connect* timeout (nothing written, retryable) or a *read* timeout
  (request sent, ambiguous). The type is identical. §15 puts read timeouts in ``UNKNOWN``.
- An :class:`ssl.SSLError` may be a handshake failure (nothing written) or a mid-stream failure
  after the request was sent.

So an adapter that knows which it had says so, by raising :class:`LedgerTransportError` with an
explicit :class:`RetryableCause`. An adapter that does not know says nothing, and the answer is
``UNKNOWN``. That asymmetry is the whole design: **the burden of proof is on the claim that nothing
was sent**, never on the claim that something might have been.
"""

from __future__ import annotations

import dataclasses
import enum
import socket
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:  # pragma: no cover - imported for typing only, and to avoid a cycle
    from ledger_exception_control_plane.ledger.port import (
        LedgerAdapter,
        LedgerAdapterCapabilities,
        PostingInstruction,
        PostingOutcome,
    )

__all__ = [
    "RETRYABLE_CAUSES",
    "AdapterCallError",
    "AttributedAdapter",
    "LedgerTransportError",
    "RetryableCause",
    "TransportClass",
    "TransportVerdict",
    "classify_transport_failure",
]


class RetryableCause(enum.StrEnum):
    """The four transport failures §15 allows a retry after, and no others.

    Enumerated verbatim from the specification's list. A fifth member added here would be a change
    to the retry contract, which is why they are named rather than described — a reviewer can
    compare this enumeration with §15 line by line.
    """

    #: DNS resolution failure. The name never resolved, so no connection was attempted.
    DNS_RESOLUTION = "dns_resolution"

    #: TCP connect failure or refusal. The peer refused or was unreachable; no request bytes exist.
    TCP_CONNECT = "tcp_connect"

    #: TLS handshake failure. The tunnel never completed, so the request was never written into it.
    TLS_HANDSHAKE = "tls_handshake"

    #: Connect-timeout **before the first byte was written**. Distinct from a read timeout, which is
    #: ambiguous and is not on this list.
    CONNECT_TIMEOUT = "connect_timeout"


#: The allowlist as a set, for callers that want to test membership rather than branch.
RETRYABLE_CAUSES: Final[frozenset[RetryableCause]] = frozenset(RetryableCause)


class TransportClass(enum.StrEnum):
    """What a transport failure means for the *financial* question, which is the only one that
    matters here: did anything reach the ledger?

    Two values, deliberately. This is not a general error taxonomy — it is the answer to "may this
    irreversible write be repeated", and that question has exactly two safe answers.
    """

    #: Nothing was written. Retry is permitted, bounded by the policy in
    #: :mod:`~ledger_exception_control_plane.operations.retry`.
    NOT_SENT = "not_sent"

    #: Anything else. §13.5's ambiguous case: never retried by the ordinary path, never coerced to
    #: success or to failure. The capability branch and manual recovery are 4.4's.
    UNKNOWN = "unknown"


@dataclasses.dataclass(frozen=True, slots=True)
class TransportVerdict:
    """The classifier's answer, carrying why it decided.

    ``cause`` is present only for :attr:`TransportClass.NOT_SENT`; an ``UNKNOWN`` verdict has no
    cause by construction, because "we do not know what happened" is precisely the state of not
    having one. A reviewer asked what an ``UNKNOWN`` with a cause would mean, and the answer was
    that it would mean the classifier had guessed.
    """

    classification: TransportClass
    cause: RetryableCause | None
    #: A short, bounded, non-sensitive description: the exception's class name, never its message.
    #: The message can carry a URL, a host, a header or a credential, and this string is persisted
    #: in the dead-letter envelope.
    detail: str

    def __post_init__(self) -> None:
        if self.classification is TransportClass.NOT_SENT and self.cause is None:
            raise ValueError("a NOT_SENT verdict must name which allowlisted cause it observed")
        if self.classification is TransportClass.UNKNOWN and self.cause is not None:
            raise ValueError("an UNKNOWN verdict cannot name a cause; it is the absence of one")

    @property
    def retryable(self) -> bool:
        """Whether the ordinary retry path may take this failure.

        Reads the classification rather than the cause, so a cause added to the enum without a
        decision about retryability cannot silently become retryable.
        """
        return self.classification is TransportClass.NOT_SENT


class LedgerTransportError(Exception):
    """Raised by an adapter that knows its request never left the client.

    **The explicit seam, and the only way to reach three of the four causes.** §15's allowlist is
    defined by "no byte of the request was written", which is a fact about the adapter's own
    transport, not about the exception type it produced. An adapter that has that fact declares it;
    one that does not stays silent and gets ``UNKNOWN``.

    A real HTTP adapter would map its client library's connect-phase exceptions onto this. The
    reference adapter is in-process and raises none, which is why the classifier's tests raise them
    directly — the seam is the thing under test.
    """

    def __init__(self, cause: RetryableCause, detail: str = "") -> None:
        super().__init__(f"{cause.value}: {detail}" if detail else cause.value)
        self.cause = cause


#: Exception types whose meaning is unambiguous: they can only occur before any request byte exists.
#:
#: Two entries, and the shortness is the point. ``ConnectionRefusedError`` is raised by the connect
#: call itself, and :class:`socket.gaierror` by resolution, which precedes connect. Neither can
#: occur once a request is on the wire.
#:
#: ``TimeoutError`` and :class:`ssl.SSLError` are deliberately **absent**: each covers both a
#: pre-first-byte case and a post-send case with the same type, so recognising them here would
#: classify an ambiguous financial write as safe to repeat. They reach this classifier as
#: ``UNKNOWN`` unless the adapter declares a cause.
_UNAMBIGUOUS: Final[tuple[tuple[type[BaseException], RetryableCause], ...]] = (
    (socket.gaierror, RetryableCause.DNS_RESOLUTION),
    (ConnectionRefusedError, RetryableCause.TCP_CONNECT),
)


def classify_transport_failure(error: BaseException) -> TransportVerdict:
    """Classify an exception raised instead of a posting outcome.

    Total, and defaults to :attr:`TransportClass.UNKNOWN` for everything it does not recognise —
    which is the direction §15 requires and the opposite of what a conventional retry classifier
    does. A new exception type appearing in a dependency therefore becomes *less* retryable, never
    more.

    The order matters: a declared cause is read first, because an adapter that knows what happened
    outranks any inference from the type. ``socket.gaierror`` is a subclass of ``OSError`` and
    ``ConnectionRefusedError`` of ``ConnectionError``, so the tuple is scanned most-specific-first
    rather than relying on ``isinstance`` ordering by accident.
    """
    if isinstance(error, LedgerTransportError):
        return TransportVerdict(
            classification=TransportClass.NOT_SENT,
            cause=error.cause,
            detail=type(error).__name__,
        )

    for exception_type, cause in _UNAMBIGUOUS:
        if isinstance(error, exception_type):
            return TransportVerdict(
                classification=TransportClass.NOT_SENT,
                cause=cause,
                detail=type(error).__name__,
            )

    return TransportVerdict(
        classification=TransportClass.UNKNOWN,
        cause=None,
        detail=type(error).__name__,
    )


# ======================================================================================
# Attribution: which exceptions this classifier is entitled to judge
# ======================================================================================


class AdapterCallError(Exception):
    """Carries an exception the **ledger adapter** raised, and nothing else.

    **The narrowest fix for the worst defect increment 4.3 shipped.** The retry module wrapped a
    whole dispatch — three database transactions with the socket write in the middle — in
    ``except Exception`` and handed whatever it caught to :func:`classify_transport_failure`.
    SQLAlchemy with asyncpg surfaces a database connect failure as a bare ``ConnectionRefusedError``
    or ``socket.gaierror``, and this classifier recognises both, correctly, as *ledger* transport
    failures that wrote nothing.

    So a PostgreSQL blip in the window §12.1.1 deliberately opens — after the posting was applied,
    before the outcome was recorded — was written down as ``NOT_SENT``: a positive assertion that
    the ledger had never been contacted, over the top of the evidence, followed by a reschedule.
    Both gates that stop a second send read exactly the two facts that write falsified.

    The classifier was never wrong. The call site asked it about an exception it was never scoped to
    judge, and this type is what makes "the adapter raised this" a fact rather than an assumption.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(f"the ledger adapter raised {type(error).__name__}")
        self.error = error


class AttributedAdapter:
    """An adapter proxy that labels the exceptions its ``post`` raises, and changes nothing else.

    Structural, like every adapter in this project: ``name`` and ``capabilities`` are forwarded
    untouched, and only ``post`` is wrapped, because ``post`` is the only call that can fail
    *instead of* answering.

    **It is deliberately transparent to the conformance record**, and that took a second correction.
    The first version was a plain wrapper, and :func:`~.conformance.implementation_of` keys evidence
    on the implementation class — so wrapping the reference ledger produced an unrecognised class,
    every proven capability was downgraded to ``NONE``, and a re-send that §13.5 permits under a
    verified ``ENFORCES_KEY`` was refused instead. A silent withdrawal of the exact claim 4.2 built
    the conformance suite to establish, and only a test asserting the *permitted* branch caught it.

    Unwrapping is safe here precisely because this class cannot be impersonated:
    ``implementation_of`` unwraps on an **exact type check**, never ``isinstance``, so a subclass
    that overrode ``post`` could not use it to inherit an inner adapter's evidence. And the
    delegation is real — the calls genuinely reach the wrapped adapter, so its record genuinely
    applies.
    """

    def __init__(self, adapter: LedgerAdapter) -> None:
        self.wrapped = adapter

    @property
    def name(self) -> str:
        return self.wrapped.name

    def capabilities(self) -> LedgerAdapterCapabilities:
        return self.wrapped.capabilities()

    async def post(self, operation_id: str, instruction: PostingInstruction) -> PostingOutcome:
        try:
            return await self.wrapped.post(operation_id, instruction)
        except Exception as error:
            raise AdapterCallError(error) from error
