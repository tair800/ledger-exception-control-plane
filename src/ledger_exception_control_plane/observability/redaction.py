"""The redaction every attribute and every log field passes through. §16 and §17.

§16: *"No secrets in logs or traces; merchant identifiers redacted in telemetry."* §17: no secret
in code, fixtures or cassettes. This module is the single gate that makes the first half true for
the telemetry signal, and :mod:`~ledger_exception_control_plane.observability.instrumentation` is
written so there is no route to a sink that bypasses it.

**Three mechanisms, because one is not enough.**

1. **Denied keys are dropped.** Some values must never be in telemetry regardless of how they are
   spelled — an amount, a merchant reference, an approval token. Pattern-matching a value cannot
   catch these: a merchant reference looks like an ordinary string and an amount looks like an
   ordinary number. The key is the only thing that identifies them, so the key is what is checked.
2. **String values are scrubbed by pattern.** A key that is legitimate can still receive a value
   that is not: a DSN with a password in it, a bearer token echoed from a header, an exception
   message carrying a request URL. ``log.py`` already declines to emit tracebacks for this exact
   reason.
3. **Types that cannot appear are refused.** A ``Decimal`` is dropped whatever key it arrives
   under, because the only ``Decimal`` in this system is a ledger amount. Anything exposing
   ``get_secret_value`` is dropped without being read, so a ``SecretStr`` cannot be unwrapped by
   accident. No module on the emission path calls that method — the one call in the whole package is
   in ``langfuse.py``, where building an HTTP Basic header genuinely requires the value, and a test
   asserts that it is the only one.

**A removal is recorded.** :class:`Redacted` carries the keys it dropped, and the instrumentation
puts them on the span under ``lecp.redacted``. Key names are not sensitive, and without them a
removal is indistinguishable from a value nobody set — which would make this module unfalsifiable
from the outside. It is the same reasoning as the ``[redacted]`` marker: a visible mark rather than
a silent deletion.

**Deliberately not reusing ``llm.cassette.redact_text``.** That function is correct for what it
guards and is *not* correct here, in both directions. It does not touch a DSN password or a merchant
reference — a test in the cassette suite asserts that merchant text *survives*, because an evidence
pack is meant to carry it — and it lives in a package whose import graph pulls in the ORM, which
this package must stay out of so that instrumenting a module cannot create an import cycle with it.
Portfolio conventions are copied, not imported; the patterns below are the cassette set widened for
what a span can leak.
"""

from __future__ import annotations

import dataclasses
import decimal
import re
from collections.abc import Mapping, Sequence
from typing import Final

from ledger_exception_control_plane.observability.conventions import (
    ATTRIBUTE_DENY_LIST,
    SPAN_ATTRIBUTE_MAX_LENGTH,
    AttributeValue,
)

__all__ = [
    "REDACTED",
    "TRUNCATED",
    "Redacted",
    "key_is_denied",
    "redact_attributes",
    "redact_text",
]

#: What a scrubbed value becomes. A visible marker rather than a deletion, so a reader can see that
#: something was removed and a test can assert on it. Spelled to match the cassette harness's
#: ``[scrubbed]`` in intent, distinct in text so a leak can be traced to the layer that caught it.
REDACTED: Final = "[redacted]"

#: Appended to a value cut at :data:`SPAN_ATTRIBUTE_MAX_LENGTH`.
TRUNCATED: Final = "…[truncated]"

#: Substrings that make a key denied wherever they appear in it.
#:
#: Whole-segment matching alone was not enough: it admits ``lecp.merchant_ref``,
#: ``lecp.approval.token_sha`` and ``lecp.provider_api_key``, all of which are the thing the fence
#: exists to stop and none of which is a bare denied word. The cost of a false positive here is an
#: attribute a dashboard does not get; the cost of a miss is a merchant identifier in a trace, which
#: §16 forbids outright.
_DENIED_SUBSTRINGS: Final[frozenset[str]] = frozenset(
    {
        "apikey",
        "api_key",
        "authorization",
        "bearer",
        "credential",
        "merchant",
        "password",
        "secret",
        "token",
    }
)

#: Namespace prefixes stripped before a key is judged, so ``lecp.amount`` is denied exactly as
#: ``amount`` is.
_NAMESPACES: Final[tuple[str, ...]] = ("lecp.", "gen_ai.")

#: A credential inside a connection string: ``scheme://user:password@host``.
#:
#: The single most likely secret to reach a span, because a DSN is what every dependency is
#: configured with and ``Settings`` holds two of them. Only the password is replaced — the scheme,
#: the user and the host are operationally useful and are not credentials.
_DSN_PASSWORD: Final = re.compile(r"(?P<head>[a-z][a-z0-9+.\-]*://[^:@/\s]*:)[^@/\s]+@", re.I)

#: Values shaped like a credential, whatever key they arrived under.
#:
#: The cassette harness's set, with the bearer threshold lowered: that module guards a file a human
#: reviews before commit, this one guards a stream nobody reviews at all, so the trade between a
#: false positive and a miss sits further towards caution.
_SECRET_SHAPED: Final = re.compile(
    r"(sk-ant-[A-Za-z0-9_\-]{12,}"
    r"|sk-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r"|AIza[A-Za-z0-9_\-]{20,}"
    r"|AKIA[A-Z0-9]{12,}"
    r"|ghp_[A-Za-z0-9]{20,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})",
    re.IGNORECASE,
)


def key_is_denied(key: str) -> bool:
    """Whether an attribute key may never carry a value into telemetry.

    Case-insensitive, namespace-stripped, and checked two ways: any dot-separated segment equal to a
    denied word, or any denied substring anywhere in the key.
    """
    lowered = key.lower()
    for namespace in _NAMESPACES:
        lowered = lowered.removeprefix(namespace)

    if any(segment in ATTRIBUTE_DENY_LIST for segment in lowered.split(".")):
        return True
    return any(fragment in lowered for fragment in _DENIED_SUBSTRINGS)


def redact_text(text: str) -> str:
    """Scrub a free string and cap its length.

    Exposed because a secret escapes through more channels than a span attribute: an exception
    message embeds a request URL, an adapter's error detail embeds an endpoint, and both end up in a
    log line. Callers outside this package may apply it to any field they are about to log.
    """
    scrubbed = _SECRET_SHAPED.sub(REDACTED, _DSN_PASSWORD.sub(rf"\g<head>{REDACTED}@", text))
    if len(scrubbed) > SPAN_ATTRIBUTE_MAX_LENGTH:
        return scrubbed[:SPAN_ATTRIBUTE_MAX_LENGTH] + TRUNCATED
    return scrubbed


def _looks_like_a_secret_wrapper(value: object) -> bool:
    """Whether a value is a secret container such as ``pydantic.SecretStr``.

    Duck-typed rather than imported, so this module needs no dependency to recognise one and so it
    catches any wrapper a later project introduces. The value is never unwrapped: recognising it is
    the whole operation, and reading it would be the leak.
    """
    return callable(getattr(value, "get_secret_value", None))


@dataclasses.dataclass(frozen=True, slots=True)
class Redacted:
    """A cleaned attribute mapping, and the keys that did not survive it."""

    attributes: dict[str, AttributeValue]

    #: Keys removed, sorted. Recorded on the span so a removal is visible rather than silent.
    dropped: tuple[str, ...]


def _redact_value(value: object) -> AttributeValue | None:
    """One value, or ``None`` if it may not be recorded at all.

    The order matters. Secret wrappers and ``Decimal`` are refused before any coercion, because
    ``str()`` on either produces exactly the string that must not exist — Pydantic renders a
    ``SecretStr`` as asterisks today, but relying on another library's ``__str__`` for a security
    property is relying on a decision somebody else may revisit.
    """
    if value is None or _looks_like_a_secret_wrapper(value):
        return None
    if isinstance(value, decimal.Decimal | bytes | bytearray):
        # A Decimal is a ledger amount and a bytes is a payload. Neither is a label.
        return None
    if isinstance(value, bool):
        # Before int: bool is a subclass of it, and False would otherwise be recorded as 0.
        return value
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        # A nested structure is a payload, not a label. Flatten it at the call site or leave it out.
        return None
    if isinstance(value, Sequence):
        members = [_redact_value(item) for item in value]
        if any(member is None for member in members):
            return None
        return tuple(str(member) for member in members)
    return redact_text(str(value))


def redact_attributes(attributes: Mapping[str, object]) -> Redacted:
    """Apply every rule to a whole mapping. The only way attributes reach a sink.

    Empty keys are dropped rather than raising: this runs on the emission path, and telemetry that
    can fail a business operation is worse than telemetry that loses an attribute.
    """
    kept: dict[str, AttributeValue] = {}
    dropped: list[str] = []

    for key, value in attributes.items():
        if not key or key_is_denied(key):
            dropped.append(key or "<empty>")
            continue
        redacted = _redact_value(value)
        if redacted is None:
            dropped.append(key)
            continue
        kept[key] = redacted

    return Redacted(attributes=kept, dropped=tuple(sorted(dropped)))
