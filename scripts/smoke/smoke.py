"""Post-deploy smoke checks for a running instance of the control plane.

Run against any base URL — the local Compose stack, staging, or production:

    python scripts/smoke/smoke.py --base-url http://localhost:8000
    python scripts/smoke/smoke.py --base-url https://<app>.fly.dev --environment staging

**Standard library only, and deliberately.** This file has to be runnable in the deployed
container, in a CI step that has not installed the project, and on a maintainer's machine with
nothing but a Python interpreter. A smoke tool that needs its own dependency tree resolved before
it can tell you whether the deployment is alive is a smoke tool you cannot use in the moment you
need it.

**What these checks are for.** Every one of them asserts a property that could plausibly be lost by
a *deployment* rather than by a code change: the process is up, the correlation middleware is
actually in the request path, the readiness probe still reports honestly, the authentication
boundary survived, no connection string leaks out of a public endpoint, and interactive API docs
are suppressed where they must be. None of them re-tests business logic — the test suite owns that,
and repeating it here against a live financial control plane would mean writing to it.

**Nothing here writes.** Every request is a GET. A smoke test that exercises the approval or
dispatch path against a deployed environment would be recording human decisions and offering
postings to a ledger, so the authenticated read path is as far as this goes.

The checks are falsifiable: ``scripts/smoke/selftest.py`` serves crafted responses from a local
stub and asserts each check goes red on the specific defect it exists to catch.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import Final

#: Liveness. Probes nothing external, so it stays green while a dependency is down (ADR-015).
LIVENESS_PATH: Final = "/healthz"

#: Readiness. Probes PostgreSQL and Redis and answers 503 when either is unavailable.
READINESS_PATH: Final = "/readyz"

#: The exception queue: the one read path these checks exercise. Requires a principal.
QUEUE_PATH: Final = "/api/v1/exceptions"

#: Interactive documentation, which the application suppresses in production.
DOCS_PATH: Final = "/docs"

#: What a correlation id may be, per ``config.CORRELATION_ID_PATTERN``. An id echoed back to us
#: that does not satisfy this means the sanitising middleware is not in the deployed request path.
CORRELATION_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")

#: A correlation id the service must refuse to trust: spaces are outside the permitted alphabet.
#: Sent verbatim to prove the service replaces it rather than reflecting untrusted input into its
#: own log stream. Carries no CR or LF, because ``http.client`` refuses to send those at all.
HOSTILE_CORRELATION_ID: Final = "smoke id with spaces"

#: Credential shapes that must never appear in a response body from a public endpoint. A readiness
#: payload legitimately contains the word ``postgres`` as a dependency *name*, so the DSN pattern
#: matches only a URL carrying userinfo — that is the shape that leaks a password.
SECRET_SHAPES: Final = (
    (
        "a connection string with credentials",
        re.compile(r"[a-z][a-z0-9+.-]*://[^\s\"']*:[^\s\"'/]*@"),
    ),
    ("a password field", re.compile(r"(?i)\bpassword\b")),
    ("a private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("a bearer token", re.compile(r"(?i)\bauthorization\b\s*[:=]")),
)


class Outcome(enum.StrEnum):
    """A check either held, did not hold, or was not applicable to this run."""

    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclasses.dataclass(frozen=True, slots=True)
class CheckResult:
    """One named check and what it found. ``detail`` is what a reader of a red run needs."""

    name: str
    outcome: Outcome
    detail: str


@dataclasses.dataclass(frozen=True, slots=True)
class Response:
    """An HTTP response, with header names lower-cased so lookups are case-insensitive."""

    status: int
    headers: dict[str, str]
    body: str

    def json(self) -> object:
        """Parse the body, raising ``ValueError`` with the offending prefix on malformed JSON."""
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as error:
            raise ValueError(f"body is not JSON ({error}): {self.body[:120]!r}") from error


@dataclasses.dataclass(frozen=True, slots=True)
class Target:
    """Everything a check needs to know about what it is probing."""

    base_url: str
    environment: str
    timeout: float
    correlation_header: str
    token: str | None


class TransportError(RuntimeError):
    """The request never produced an HTTP response — DNS, TCP, TLS or a timeout."""


def fetch(
    target: Target,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    """Issue one GET and return the response, treating a 4xx/5xx as a result rather than an error.

    ``HTTPError`` is itself a response object, which is what makes the 401 and 404 checks below
    possible without a second code path.
    """
    request = urllib.request.Request(
        url=target.base_url.rstrip("/") + path,
        method="GET",
        headers=headers or {},
    )
    try:
        with urllib.request.urlopen(request, timeout=target.timeout) as response:
            return Response(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read().decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as error:
        return Response(
            status=error.code,
            headers={key.lower(): value for key, value in error.headers.items()},
            body=error.read().decode("utf-8", errors="replace"),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise TransportError(f"GET {path} did not answer: {error}") from error


def find_secret_shape(body: str) -> str | None:
    """Return the name of the first credential shape found in ``body``, or ``None``."""
    for description, pattern in SECRET_SHAPES:
        if pattern.search(body):
            return description
    return None


# ======================================================================================
# The checks
# ======================================================================================


def check_liveness(target: Target) -> CheckResult:
    """The process is up and identifies itself, and says nothing sensitive doing so."""
    name = "liveness"
    response = fetch(target, LIVENESS_PATH)
    if response.status != 200:
        return CheckResult(name, Outcome.FAIL, f"{LIVENESS_PATH} answered {response.status}")

    payload = response.json()
    if not isinstance(payload, dict):
        return CheckResult(name, Outcome.FAIL, f"{LIVENESS_PATH} returned {type(payload).__name__}")
    if payload.get("status") != "alive":
        return CheckResult(name, Outcome.FAIL, f"status is {payload.get('status')!r}, not 'alive'")
    for field in ("service", "version"):
        if not str(payload.get(field, "")).strip():
            return CheckResult(name, Outcome.FAIL, f"{field} is missing or empty")

    leak = find_secret_shape(response.body)
    if leak is not None:
        return CheckResult(name, Outcome.FAIL, f"the liveness body contains {leak}")

    return CheckResult(
        name,
        Outcome.PASS,
        f"{payload['service']} {payload['version']} is alive",
    )


def check_correlation_id_is_echoed(target: Target) -> CheckResult:
    """A well-formed inbound correlation id comes back unchanged.

    This is the cheapest available proof that the correlation middleware is in the deployed
    request path — which is what every log line, span and audit event depends on for its identity.
    """
    name = "correlation-id-echo"
    supplied = f"smoke-{int(time.time())}"
    response = fetch(target, LIVENESS_PATH, headers={target.correlation_header: supplied})
    echoed = response.headers.get(target.correlation_header.lower())

    if echoed is None:
        return CheckResult(name, Outcome.FAIL, f"no {target.correlation_header} on the response")
    if echoed != supplied:
        return CheckResult(name, Outcome.FAIL, f"sent {supplied!r}, got {echoed!r}")
    return CheckResult(name, Outcome.PASS, f"{target.correlation_header} echoed unchanged")


def check_correlation_id_is_sanitised(target: Target) -> CheckResult:
    """A malformed inbound correlation id is replaced, not reflected.

    §16 forbids untrusted input reaching a log record verbatim. An id outside the permitted
    alphabet must be swapped for a generated one; echoing it back would mean a request header can
    write whatever it likes into the log stream.
    """
    name = "correlation-id-sanitised"
    response = fetch(
        target,
        LIVENESS_PATH,
        headers={target.correlation_header: HOSTILE_CORRELATION_ID},
    )
    echoed = response.headers.get(target.correlation_header.lower())

    if echoed is None:
        return CheckResult(name, Outcome.FAIL, f"no {target.correlation_header} on the response")
    if echoed == HOSTILE_CORRELATION_ID:
        return CheckResult(name, Outcome.FAIL, "a malformed correlation id was reflected verbatim")
    if not CORRELATION_ID_PATTERN.match(echoed):
        return CheckResult(
            name, Outcome.FAIL, f"the replacement id is itself malformed: {echoed!r}"
        )
    return CheckResult(name, Outcome.PASS, "a malformed correlation id was replaced")


def check_readiness(target: Target, *, require_ready: bool) -> CheckResult:
    """Dependencies are reachable, and the payload names them without describing them.

    ``require_ready=False`` exists for running this file against an application started without
    its dependencies — a local process with no database. It is never used by the pipeline, where a
    deployment that cannot reach its database is a failed deployment.
    """
    name = "readiness"
    response = fetch(target, READINESS_PATH)

    if response.status not in (200, 503):
        return CheckResult(name, Outcome.FAIL, f"{READINESS_PATH} answered {response.status}")

    payload = response.json()
    if not isinstance(payload, dict):
        return CheckResult(
            name, Outcome.FAIL, f"{READINESS_PATH} returned {type(payload).__name__}"
        )

    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list) or not dependencies:
        return CheckResult(name, Outcome.FAIL, "the readiness payload names no dependencies")

    reported = ", ".join(
        f"{item.get('name')}={item.get('status')}"
        for item in dependencies
        if isinstance(item, dict)
    )

    leak = find_secret_shape(response.body)
    if leak is not None:
        return CheckResult(name, Outcome.FAIL, f"the readiness body contains {leak}")

    if response.status == 503 or payload.get("status") != "ready":
        detail = f"not ready: {reported}"
        return CheckResult(name, Outcome.FAIL if require_ready else Outcome.SKIP, detail)

    return CheckResult(name, Outcome.PASS, f"ready: {reported}")


def check_queue_refuses_anonymous(target: Target) -> CheckResult:
    """The read path refuses an unauthenticated caller, and says which scheme to use.

    A control plane whose queue is world-readable has leaked its merchants' settlement positions,
    so this is checked on every deployment rather than trusted to configuration.
    """
    name = "queue-refuses-anonymous"
    response = fetch(target, f"{QUEUE_PATH}?limit=1")

    if response.status != 401:
        return CheckResult(
            name,
            Outcome.FAIL,
            f"{QUEUE_PATH} answered {response.status} to an anonymous caller, not 401",
        )
    if "bearer" not in response.headers.get("www-authenticate", "").lower():
        return CheckResult(name, Outcome.FAIL, "the 401 carries no WWW-Authenticate: Bearer")
    return CheckResult(name, Outcome.PASS, "anonymous reads are refused with 401")


def check_queue_read(target: Target, *, required: bool) -> CheckResult:
    """One real read path, end to end: token accepted, database queried, queue serialised.

    Skipped without a token so this file stays runnable by anyone; ``--require-queue-read`` makes
    the absence of a token a failure, which is what the pipeline passes.
    """
    name = "queue-read"
    if not target.token:
        detail = "no token supplied (SMOKE_TOKEN / --token)"
        return CheckResult(name, Outcome.FAIL if required else Outcome.SKIP, detail)

    response = fetch(
        target,
        f"{QUEUE_PATH}?limit=5",
        headers={"Authorization": f"Bearer {target.token}"},
    )
    if response.status == 401:
        return CheckResult(name, Outcome.FAIL, "the supplied token is not a configured principal")
    if response.status != 200:
        return CheckResult(name, Outcome.FAIL, f"{QUEUE_PATH} answered {response.status}")

    payload = response.json()
    if not isinstance(payload, list):
        return CheckResult(name, Outcome.FAIL, f"the queue returned {type(payload).__name__}")

    if payload:
        first = payload[0]
        if not isinstance(first, dict):
            return CheckResult(name, Outcome.FAIL, "a queue entry is not an object")
        missing = {"id", "classification", "status", "correlation_id"} - set(first)
        if missing:
            return CheckResult(name, Outcome.FAIL, f"a queue entry is missing {sorted(missing)}")

    return CheckResult(name, Outcome.PASS, f"the queue answered with {len(payload)} exception(s)")


def check_interactive_docs_suppressed(target: Target) -> CheckResult:
    """Production serves no interactive API documentation.

    ``create_app`` sets ``docs_url=None`` when the environment is production. Checked here because
    the thing that decides it is an environment variable, and a wrong one is invisible until
    somebody finds the page.
    """
    name = "docs-suppressed"
    if target.environment != "production":
        return CheckResult(
            name, Outcome.SKIP, f"only asserted in production (got {target.environment})"
        )

    response = fetch(target, DOCS_PATH)
    if response.status == 200:
        return CheckResult(name, Outcome.FAIL, f"{DOCS_PATH} is served in production")
    return CheckResult(name, Outcome.PASS, f"{DOCS_PATH} answered {response.status}")


def run_checks(
    target: Target, *, require_ready: bool, require_queue_read: bool
) -> list[CheckResult]:
    """Run every check in order, collecting results rather than stopping at the first failure.

    Collecting is the point: told only that liveness failed, an operator learns nothing about
    whether the database is reachable, and post-deploy is exactly when the whole picture is wanted.
    A transport error is recorded against the check that hit it and the run continues.
    """
    checks: tuple[tuple[str, Callable[[], CheckResult]], ...] = (
        ("liveness", lambda: check_liveness(target)),
        ("correlation-id-echo", lambda: check_correlation_id_is_echoed(target)),
        ("correlation-id-sanitised", lambda: check_correlation_id_is_sanitised(target)),
        ("readiness", lambda: check_readiness(target, require_ready=require_ready)),
        ("queue-refuses-anonymous", lambda: check_queue_refuses_anonymous(target)),
        ("queue-read", lambda: check_queue_read(target, required=require_queue_read)),
        ("docs-suppressed", lambda: check_interactive_docs_suppressed(target)),
    )

    results: list[CheckResult] = []
    for name, check in checks:
        try:
            results.append(check())
        except (TransportError, ValueError) as error:
            results.append(CheckResult(name, Outcome.FAIL, str(error)))
    return results


def wait_for_liveness(target: Target, *, deadline_seconds: float) -> str | None:
    """Poll liveness until it answers 200 or the deadline passes; return the last error.

    A freshly released machine can take a few seconds to accept connections, and a smoke run that
    raced the rollout would fail for the one reason that is not a defect.
    """
    if deadline_seconds <= 0:
        return None

    deadline = time.monotonic() + deadline_seconds
    last = "liveness never answered"
    while True:
        try:
            if fetch(target, LIVENESS_PATH).status == 200:
                return None
            last = "liveness did not answer 200"
        except TransportError as error:
            last = str(error)
        if time.monotonic() >= deadline:
            return f"{last} within {deadline_seconds:.0f}s"
        time.sleep(min(2.0, max(0.5, deadline - time.monotonic())))


def render(results: Sequence[CheckResult]) -> str:
    """One aligned line per check. Read by a human looking at a failed deployment."""
    symbols = {Outcome.PASS: "PASS", Outcome.FAIL: "FAIL", Outcome.SKIP: "SKIP"}
    width = max((len(result.name) for result in results), default=0)
    return "\n".join(
        f"  {symbols[result.outcome]}  {result.name.ljust(width)}  {result.detail}"
        for result in results
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python scripts/smoke/smoke.py",
        description="Post-deploy smoke checks against a running control plane.",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SMOKE_BASE_URL", "http://localhost:8000"),
        help="base URL of the instance to check (env: SMOKE_BASE_URL)",
    )
    parser.add_argument(
        "--environment",
        default=os.environ.get("SMOKE_ENVIRONMENT", "local"),
        choices=("local", "ci", "staging", "production"),
        help="which environment this is; production asserts documentation is suppressed",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SMOKE_TOKEN"),
        help=(
            "bearer token for a configured principal, for the authenticated read "
            "(env: SMOKE_TOKEN). Never pass a value on a shared command line."
        ),
    )
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout, seconds")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=float(os.environ.get("SMOKE_WAIT_SECONDS", "60")),
        help="how long to wait for liveness before running the checks",
    )
    parser.add_argument(
        "--correlation-header",
        default=os.environ.get("SMOKE_CORRELATION_HEADER", "X-Request-ID"),
        help="the header carrying the correlation id, if the deployment overrides it",
    )
    parser.add_argument(
        "--allow-not-ready",
        action="store_true",
        help=(
            "downgrade a failing readiness probe to a skip. For running this against an "
            "application started without its dependencies; never used by the pipeline."
        ),
    )
    parser.add_argument(
        "--require-queue-read",
        action="store_true",
        help="fail rather than skip when no token is available for the authenticated read",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)

    if not arguments.base_url.startswith(("http://", "https://")):
        print(f"--base-url must be http or https, got {arguments.base_url!r}", file=sys.stderr)
        return 2

    target = Target(
        base_url=arguments.base_url,
        environment=arguments.environment,
        timeout=arguments.timeout,
        correlation_header=arguments.correlation_header,
        token=arguments.token,
    )

    print(f"smoke: {target.base_url} ({target.environment})")

    unreachable = wait_for_liveness(target, deadline_seconds=arguments.wait_seconds)
    if unreachable is not None:
        print(f"  FAIL  liveness  {unreachable}")
        print("smoke: FAILED (the instance never became reachable)")
        return 1

    results = run_checks(
        target,
        require_ready=not arguments.allow_not_ready,
        require_queue_read=arguments.require_queue_read,
    )
    print(render(results))

    failures = [result for result in results if result.outcome is Outcome.FAIL]
    skipped = [result for result in results if result.outcome is Outcome.SKIP]
    summary = f"{len(results) - len(failures) - len(skipped)} passed, {len(failures)} failed"
    if skipped:
        summary += f", {len(skipped)} skipped"

    if failures:
        print(f"smoke: FAILED ({summary})")
        return 1
    print(f"smoke: OK ({summary})")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
