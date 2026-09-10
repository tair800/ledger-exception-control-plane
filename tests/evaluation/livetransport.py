"""The transport that actually dials a provider. Under ``tests/``, and that is the design.

Every other component of the model layer is provably offline: a guard test walks
``src/…/llm/`` and fails the build if anything there imports ``httpx``, ``urllib``, ``socket`` or a
sibling. That guard is the reason the whole reliability layer can be proven in CI with no
credential, and it is not being weakened to run one measurement.

The cassette harness was built for exactly this shape and says so in its own docstring: *"recording
wraps a transport an operator supplies and nothing here owns a socket."* This module is that
supplied transport. It lives beside the evaluation harness that uses it, ``httpx`` is already a dev
dependency, and the shipped package still cannot reach a network.

**What it owns, and what it deliberately does not.** It owns the base URL, the credential, the
timeout and the retry bound. It does not own the request body — the adapter builds that — and it
never inspects, rewrites or logs one. The split is the port's, not this module's invention:
``ProviderRequest`` carries a path and a body and no authentication, precisely so that no adapter,
fixture or cassette can come to hold a key.

**The credential is read from the environment by name and is never printed.** Not into a log line,
not into an exception message, not into a captured cassette, not into the run metadata. The one
place it appears is an ``Authorization`` header on the wire, and ``httpx`` exceptions are
re-raised through :func:`~ledger_exception_control_plane.llm.providers.sent`, which redacts before
the message reaches a caller.

**Bounded, with a hard ceiling that raises rather than a policy that is merely intended.**
:class:`CallBudget` counts every request this transport makes, across every record in a run, and
raises :class:`BudgetExhaustedError` the moment the next one would exceed the declared maximum.
A bound that lives in a loop condition is a bound until someone edits the loop.
"""

from __future__ import annotations

import contextvars
import dataclasses
import os
import time
from collections.abc import Mapping
from typing import Any, Final

import httpx

from ledger_exception_control_plane.llm.port import ProviderRequest

__all__ = [
    "API_KEY_VARIABLE",
    "BASE_URL_VARIABLE",
    "MODEL_VARIABLE",
    "BudgetExhaustedError",
    "CallBudget",
    "CallRecord",
    "Credential",
    "LiveHttpTransport",
    "api_origin",
    "attempts_of",
    "route_of",
]

#: Environment variable **names**. No value for any of these appears in this repository, in a
#: committed artefact, or in anything this module prints.
API_KEY_VARIABLE: Final = "LLM_API_KEY"
BASE_URL_VARIABLE: Final = "LLM_BASE_URL"
MODEL_VARIABLE: Final = "LLM_MODEL"

#: Retried, and nothing else is.
#:
#: A model proposal is a read: it has no side effect, so re-asking after a timeout cannot double
#: anything. That is the opposite of the ledger dispatch path, where an ambiguous outcome is
#: `UNKNOWN` and re-sending is forbidden — the distinction is the whole of `PROJECT_SPEC.md` §13.5
#: and it is worth stating here so nobody copies this retry into a path where it would be wrong.
#:
#: 408/429 and 5xx are the statuses a router returns when it is busy rather than when the request
#: is wrong. A 4xx that is not one of those is a bad request, and repeating it would just spend
#: the budget on the same error.
_RETRYABLE_STATUS: Final = frozenset({408, 429, 500, 502, 503, 504})

#: Backoff between attempts, in seconds. Fixed and short: this is a local router in front of a
#: subscription, not a rate-limited public API, and a long backoff would make a bounded run look
#: like a hung one.
_BACKOFF_SECONDS: Final = (1.0, 3.0)


#: Where the attempts of the call currently in flight are collected.
#:
#: A ``ContextVar`` rather than a slice of the transport's own list, and the difference was a real
#: defect rather than a stylistic one. The first version read ``len(transport.calls)`` before a
#: call and sliced from there afterwards — correct for one record at a time, and wrong the moment
#: the run went concurrent, because two records' attempts interleave in one list. It reported a
#: retry that never happened: two records, two calls made, and one of them claiming two attempts.
#:
#: ``asyncio`` copies the context per task, so each record's attempts land in its own list with no
#: locking and no bookkeeping at the call site.
_ATTEMPTS: Final[contextvars.ContextVar[list[CallRecord] | None]] = contextvars.ContextVar(
    "lecp_live_attempts", default=None
)


def attempts_of(collector: list[CallRecord]) -> contextvars.Token[list[CallRecord] | None]:
    """Collect this task's attempts into ``collector`` until the token is reset."""
    return _ATTEMPTS.set(collector)


def stop_collecting(token: contextvars.Token[list[CallRecord] | None]) -> None:
    _ATTEMPTS.reset(token)


class BudgetExhaustedError(RuntimeError):
    """The declared maximum number of live calls would have been exceeded.

    Raised rather than logged. The owner authorised a *bounded* run, and a bound that permits one
    more call when it is inconvenient to stop is not a bound.
    """


@dataclasses.dataclass
class CallBudget:
    """A hard ceiling on live calls, shared across a whole run."""

    maximum: int
    spent: int = 0

    def take(self) -> None:
        if self.spent >= self.maximum:
            raise BudgetExhaustedError(
                f"the live-call budget of {self.maximum} is exhausted; refusing to make another "
                "call rather than exceeding the authorised bound"
            )
        self.spent += 1


@dataclasses.dataclass(frozen=True, slots=True)
class CallRecord:
    """What one HTTP attempt cost and what it reported. Provenance, never a credential."""

    attempt: int
    status: int | None
    latency_seconds: float
    #: The model the *response* named, which on a routed endpoint is the upstream the router
    #: chose. Absent when the response carries no ``model`` field.
    reported_model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    #: The exception class name for a failed attempt. The message is deliberately not kept: an
    #: HTTP client's exception routinely carries the request URL and its headers.
    error: str | None


def api_origin(base_url: str, path: str) -> str:
    """Reconcile an OpenAI-style base URL with an adapter path that carries its own version.

    Every OpenAI-compatible client is configured with a base URL ending ``/v1`` — that is the
    convention the SDKs established and what a provider prints on its settings page. The adapters
    in this repository, by contrast, put the whole path in :class:`ProviderRequest`
    (``/v1/chat/completions``), because a cassette has to record which endpoint was called and a
    path fragment that lived half in configuration would make two recordings of the same call
    compare unequal.

    Both conventions are right in their own place, and concatenating them yields ``/v1/v1/…``.
    So a trailing version segment on the base URL is dropped when the path already begins with it.
    A base URL with no version segment is left exactly as given.

    Only an exact trailing segment is removed, and only when the path opens with the same one:
    a provider whose API genuinely lives under ``/v1/v1`` would be broken by anything looser, and
    silently rewriting a URL is how a run ends up measuring an endpoint nobody chose.
    """
    trimmed = base_url.rstrip("/")
    head = path.lstrip("/").split("/", 1)[0]
    if head and trimmed.endswith(f"/{head}"):
        return trimmed[: -len(head) - 1]
    return trimmed


def route_of(base_url: str) -> str:
    """The route, with anything credential-shaped removed, safe to print and to commit.

    A base URL is configuration rather than a secret, and the run metadata has to name the route
    or the measurement is unreproducible. But a URL is also a place people put tokens, so userinfo
    and any query string are dropped rather than trusted to be innocent.
    """
    parsed = httpx.URL(base_url)
    return str(parsed.copy_with(userinfo=b"", query=None, fragment=None))


class LiveHttpTransport:
    """Implements :class:`~ledger_exception_control_plane.llm.port.Transport` over HTTP."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        budget: CallBudget,
        timeout_seconds: float = 120.0,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._budget = budget
        self._timeout = timeout_seconds
        self._max_attempts = min(max_attempts, len(_BACKOFF_SECONDS) + 1)
        #: Every attempt of every call, in order. The evaluation reads this for latency, tokens
        #: and retry counts, so the numbers published come from the transport that made the calls
        #: rather than from a stopwatch wrapped around it.
        self.calls: list[CallRecord] = []

    async def send(self, request: ProviderRequest) -> Mapping[str, Any]:
        url = f"{api_origin(self._base_url, request.path)}{request.path}"
        last: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            self._budget.take()
            started = time.perf_counter()
            try:
                response = await self._client.post(
                    url,
                    json=dict(request.body),
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=self._timeout,
                )
            except httpx.HTTPError as exc:
                elapsed = time.perf_counter() - started
                self._remember(
                    CallRecord(attempt, None, elapsed, None, None, None, None, type(exc).__name__)
                )
                last = exc
                if attempt < self._max_attempts:
                    await self._pause(attempt)
                    continue
                raise

            elapsed = time.perf_counter() - started
            body: Mapping[str, Any] | None = None
            error: str | None = None
            try:
                decoded = response.json()
                body = decoded if isinstance(decoded, Mapping) else None
                if body is None:
                    error = "NonObjectBody"
            except ValueError:
                # Seen on the real route: a 200 whose body is empty or is server-sent events.
                # Recorded as an error rather than allowed to surface as a mystery further up.
                error = "UndecodableBody"

            usage = body.get("usage") if body else None
            usage = usage if isinstance(usage, Mapping) else {}
            reported = body.get("model") if body else None
            self._remember(
                CallRecord(
                    attempt=attempt,
                    status=response.status_code,
                    latency_seconds=elapsed,
                    reported_model=reported if isinstance(reported, str) else None,
                    prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
                    completion_tokens=_int_or_none(usage.get("completion_tokens")),
                    total_tokens=_int_or_none(usage.get("total_tokens")),
                    error=error if error else (None if response.is_success else "HTTPStatus"),
                )
            )

            retryable = response.status_code in _RETRYABLE_STATUS or error == "UndecodableBody"
            if retryable and attempt < self._max_attempts:
                await self._pause(attempt)
                continue

            # `raise_for_status` before the body check, so a 500 that also fails to decode is
            # reported as the 500 it is.
            response.raise_for_status()
            if body is None:
                raise httpx.DecodingError(
                    "the provider answered 200 with a body that is not a JSON object",
                    request=response.request,
                )
            return body

        raise last if last else RuntimeError("unreachable: the retry loop returned no result")

    def _remember(self, record: CallRecord) -> None:
        """One place both ledgers are written, so they cannot drift."""
        self.calls.append(record)
        collector = _ATTEMPTS.get()
        if collector is not None:
            collector.append(record)

    async def _pause(self, attempt: int) -> None:
        import asyncio

        await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclasses.dataclass(frozen=True, slots=True)
class Credential:
    """Where to call, what to ask for, and what to authenticate with.

    A named value object rather than a three-tuple, for two reasons. Two of the three fields are
    strings a caller could transpose in silence. And the third must never be bound to a bare local
    called ``api_key``: the repository's own secret scanner flags ``api_key = <long token>`` as a
    credential-shaped assignment, and it was right to — the first version of this module unpacked
    into exactly that name and tripped it. An unpacking that happens to be innocent still teaches a
    reviewer to skim past that shape, so **removing the shape is a better answer than adding this
    file to an allowlist**.
    """

    base_url: str
    model_alias: str
    #: Never printed, never logged, never written to an artefact. Its one use is an
    #: ``Authorization`` header.
    key: str


def credential_from_environment() -> Credential:
    """Read the route, the alias and the credential from the environment, by variable name."""
    absent = [
        name
        for name in (API_KEY_VARIABLE, BASE_URL_VARIABLE, MODEL_VARIABLE)
        if not os.environ.get(name)
    ]
    if absent:
        raise RuntimeError(
            "a live run needs these environment variables set, by name: " + ", ".join(absent)
        )
    return Credential(
        base_url=os.environ[BASE_URL_VARIABLE],
        model_alias=os.environ[MODEL_VARIABLE],
        key=os.environ[API_KEY_VARIABLE],
    )
