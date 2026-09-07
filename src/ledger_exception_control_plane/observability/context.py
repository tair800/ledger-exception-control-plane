"""The identity a span carries. §18's *"present on every log line, span and audit event"*.

Every span this package emits carries a ``correlation_id``, and there is no constructor that
produces a context without one. That is the whole point: §18's claim is not "spans usually have a
correlation id", and a value threaded by convention is a value some call site will forget.

**Two kinds of correlation id already exist in this system, and conflating them would be a lie.**
``audit.correlation_id_for`` derives the canonical id from the ingested artefact — the content hash
of the file and the line's position in it — so it is stable across a crash and identical in every
stage that touches that line. ``api.py``'s middleware binds a *request-scoped* id instead, taken
from a header or generated. Only the first joins to an ``audit_event`` row. Both belong on a span,
so both are admitted, and the span records which one it has under ``lecp.correlation_source``.

This module derives no correlation id of its own. The artefact form takes the id
``audit.correlation_id_for`` produced, and the ambient form reads the one ``log.py`` has bound —
because a telemetry layer that minted its own identifier would be the one layer whose ids join to
nothing, while looking exactly like the ones that do.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Final

from ledger_exception_control_plane.config import CORRELATION_ID_MAX_LENGTH
from ledger_exception_control_plane.log import get_correlation_id, new_correlation_id
from ledger_exception_control_plane.observability.conventions import (
    Attr,
    AttributeValue,
    CorrelationSource,
)

__all__ = ["INVALID_CORRELATION_ID", "TelemetryContext"]

#: What a correlation id becomes when it fails the safety check.
#:
#: A fixed, obviously-synthetic value rather than an exception, following ``api.py``'s policy for a
#: malformed inbound header: *"a malformed header does not merit failing a request, but it must
#: never reach a log record verbatim"*. Telemetry that can fail a financial operation is worse than
#: telemetry that loses an id, and a greppable marker says what happened — the same choice
#: ``audit.UNRECORDED_CORRELATION_ID`` makes for the same reason.
INVALID_CORRELATION_ID: Final = "lecp-correlation-invalid"


def _is_safe(candidate: str) -> bool:
    """Whether an id may reach a span attribute and a log line verbatim.

    Deliberately *not* ``config.is_valid_correlation_id``. That policy governs an **inbound HTTP
    header**, which is untrusted input, and restricts it to ``[A-Za-z0-9_-]`` — an alphabet the
    canonical artefact-derived id fails, because ``lecp:<hash>:<line>`` contains colons. Reusing it
    here would reject the one correlation id that actually joins to the audit trail.
    """
    if not candidate or len(candidate) > CORRELATION_ID_MAX_LENGTH:
        return False
    # No control characters and no whitespace: those are the log-injection and line-splitting
    # characters, and they are the only reason this check exists.
    return candidate.isprintable() and not any(character.isspace() for character in candidate)


def _identifier(value: uuid.UUID | str | None) -> str | None:
    """Normalise an identifier to a string, or leave it absent."""
    if value is None:
        return None
    return str(value)


@dataclasses.dataclass(frozen=True, slots=True)
class TelemetryContext:
    """The identity every span carries. Construct through :meth:`artefact` or :meth:`ambient`.

    Frozen, because a context is a statement about which economic event the span is about, and one
    that later code can edit is one a reader cannot trust.
    """

    correlation_id: str
    source: CorrelationSource
    exception_id: str | None = None
    operation_id: str | None = None
    adjustment_id: str | None = None
    batch_id: str | None = None

    def __post_init__(self) -> None:
        if not _is_safe(self.correlation_id):
            object.__setattr__(self, "correlation_id", INVALID_CORRELATION_ID)

    @classmethod
    def artefact(
        cls,
        correlation_id: str,
        *,
        exception_id: uuid.UUID | str | None = None,
        operation_id: str | None = None,
        adjustment_id: uuid.UUID | str | None = None,
        batch_id: uuid.UUID | str | None = None,
    ) -> TelemetryContext:
        """A context whose id came from ``audit.correlation_id_for``, and so joins to the trail."""
        return cls(
            correlation_id=correlation_id,
            source=CorrelationSource.ARTEFACT,
            exception_id=_identifier(exception_id),
            operation_id=_identifier(operation_id),
            adjustment_id=_identifier(adjustment_id),
            batch_id=_identifier(batch_id),
        )

    @classmethod
    def ambient(
        cls,
        *,
        exception_id: uuid.UUID | str | None = None,
        operation_id: str | None = None,
        adjustment_id: uuid.UUID | str | None = None,
        batch_id: uuid.UUID | str | None = None,
    ) -> TelemetryContext:
        """A context taking the request- or run-scoped id ``log.py`` has bound.

        Generates one when nothing has bound an id — which is the normal case for a stage driven
        from a CLI rather than from an HTTP request, and is why those stages currently log
        ``correlation_id: null``. Generating here is not minting a competing identity: it is the
        same ``log.new_correlation_id`` the HTTP middleware calls, and the span says the id is
        ambient so nobody reads it as an artefact-derived one.
        """
        return cls(
            correlation_id=get_correlation_id() or new_correlation_id(),
            source=CorrelationSource.AMBIENT,
            exception_id=_identifier(exception_id),
            operation_id=_identifier(operation_id),
            adjustment_id=_identifier(adjustment_id),
            batch_id=_identifier(batch_id),
        )

    def attributes(self) -> dict[str, AttributeValue]:
        """The identity attributes, with the absent ones left out rather than sent as null.

        An attribute set to ``None`` is not an absent attribute, it is an attribute asserting that
        the value is null — and a dashboard cannot tell that apart from a stage that failed to
        populate it.
        """
        found: dict[str, AttributeValue] = {
            Attr.CORRELATION_ID.value: self.correlation_id,
            Attr.CORRELATION_SOURCE.value: self.source.value,
        }
        optional: tuple[tuple[Attr, str | None], ...] = (
            (Attr.EXCEPTION_ID, self.exception_id),
            (Attr.OPERATION_ID, self.operation_id),
            (Attr.ADJUSTMENT_ID, self.adjustment_id),
            (Attr.BATCH_ID, self.batch_id),
        )
        for attribute, value in optional:
            if value is not None:
                found[attribute.value] = value
        return found
