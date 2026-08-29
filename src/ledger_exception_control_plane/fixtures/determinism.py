"""The three sources of nondeterminism, each closed deliberately.

A fixture corpus that changes between runs is worse than no fixture corpus: every test built
on it becomes flaky in a way that looks like a real regression. Three things would otherwise
vary — random draws, identifiers and time — and each is replaced here with a pure function of
the seed.

**Draws are hashed, not streamed.** ``random.Random(seed)`` would be reproducible for a fixed
Python version, but it is a *stream*: a value depends on how many draws preceded it, so
inserting one field anywhere shifts everything after it, and the guarantee is only as stable
as CPython's implementation of ``shuffle`` and ``sample``, which has changed before. Every
value here is instead ``SHA-256(domain || seed || label)``, so it depends on nothing but its
own label. Adding a scenario cannot perturb another scenario's data.

**Identifiers are UUIDv5, and that is load-bearing twice over.** Version 5 is deterministic —
the same name always yields the same UUID — and it is *visibly not version 4*, which
:doc:`ADR-022 <DECISIONS>` reserves for production rows. A fixture identifier can therefore
never be mistaken for one the application generated, and a test asserts every identifier in
the corpus has version 5.

**Time is anchored, never read.** No call to ``now()``, ``today()`` or ``time()`` appears in
this package, and a test enforces that by walking the AST rather than grepping. Every
timestamp is :data:`FIXTURE_EPOCH` plus an offset the scenario states.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from typing import Final, TypeVar

T = TypeVar("T")

#: Domain-separation tag. Included in every digest so a fixture draw can never coincide with
#: a hash computed elsewhere in the system — ``operation_id`` derivation in particular, which
#: is also SHA-256 over length-prefixed components (ADR-004b).
_DOMAIN_TAG: Final = b"lecp.fixtures.v1"

#: Namespace for every fixture identifier. Built from a URL in the reserved ``.invalid`` TLD,
#: which is guaranteed by RFC 2606 never to resolve, so the namespace cannot be confused with
#: a real endpoint.
FIXTURE_UUID_NAMESPACE: Final = uuid.uuid5(
    uuid.NAMESPACE_URL, "https://example.invalid/lecp/fixtures/v1"
)

#: The corpus's fixed "now". Every timestamp and date in a generated corpus is derived from
#: this by an offset the scenario declares. Chosen to sit mid-year so a cross-period scenario
#: can straddle a month boundary in either direction without leaving the year.
FIXTURE_EPOCH: Final = dt.datetime(2026, 6, 1, 9, 0, 0, tzinfo=dt.UTC)


def _digest(domain: str, seed: int, label: str) -> int:
    """SHA-256 over length-prefixed components, as a big integer.

    Length-prefixed because plain concatenation collides: ``("ab", "c")`` and ``("a", "bc")``
    would otherwise hash identically. The same reasoning as ADR-004b, applied to a much less
    consequential value — but the habit is worth keeping consistent.
    """
    hasher = hashlib.sha256()
    hasher.update(_DOMAIN_TAG)
    for part in (domain, str(seed), label):
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
    return int.from_bytes(hasher.digest(), "big")


class Draw:
    """Deterministic value source. Not a random number generator, and not called one.

    Every method is a pure function of ``(domain, seed, label)``. There is no internal state,
    so two calls with the same label return the same value and the order of calls is
    irrelevant.

    **Honest about the distribution.** :meth:`integer` reduces a 256-bit digest modulo the
    range, which is very slightly biased toward the low end of the interval. For synthetic
    fixture data that bias is immaterial and rejection sampling would add state-dependent
    behaviour for no benefit — but it is stated rather than implied, because "uniform" is a
    claim and this is not quite one.
    """

    __slots__ = ("_domain", "_seed")

    def __init__(self, seed: int, domain: str) -> None:
        self._seed = seed
        self._domain = domain

    def child(self, domain: str) -> Draw:
        """A sub-source, so one scenario's labels cannot collide with another's."""
        return Draw(self._seed, f"{self._domain}/{domain}")

    def integer(self, label: str, low: int, high: int) -> int:
        """An integer in ``[low, high]`` inclusive."""
        if high < low:
            raise ValueError("high must not be below low")
        span = high - low + 1
        return low + _digest(self._domain, self._seed, label) % span

    def choice(self, label: str, options: Sequence[T]) -> T:
        if not options:
            raise ValueError("options must not be empty")
        return options[self.integer(label, 0, len(options) - 1)]

    def chance(self, label: str, numerator: int, denominator: int) -> bool:
        """True for ``numerator`` cases out of ``denominator``."""
        if not 0 <= numerator <= denominator or denominator <= 0:
            raise ValueError("numerator must be within [0, denominator] and denominator > 0")
        return self.integer(label, 0, denominator - 1) < numerator


def fixture_uuid(*parts: str) -> uuid.UUID:
    """A deterministic identifier for a fixture row.

    Version 5, deliberately: it is reproducible, and it is distinguishable at a glance from
    the version 4 identifiers the application generates for real rows.
    """
    if not parts:
        raise ValueError("a fixture identifier needs at least one name part")
    return uuid.uuid5(FIXTURE_UUID_NAMESPACE, "|".join(parts))


def at(days: int = 0, hours: int = 0, minutes: int = 0) -> dt.datetime:
    """A timestamp offset from :data:`FIXTURE_EPOCH`. Timezone-aware, never the wall clock."""
    return FIXTURE_EPOCH + dt.timedelta(days=days, hours=hours, minutes=minutes)


def on(days: int = 0) -> dt.date:
    """A date offset from :data:`FIXTURE_EPOCH`."""
    return at(days=days).date()
