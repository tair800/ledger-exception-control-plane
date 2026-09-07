"""Falsifiability battery for the smoke checks.

    python scripts/smoke/selftest.py

**Why this exists.** A smoke script that always passes is worse than no smoke script: it converts
"we did not look" into "we checked and it was fine". Every check in ``smoke.py`` asserts something
about a deployed instance, and none of them can be exercised by the test suite, because the suite
has no deployed instance. So the checks are exercised here against a local stub that serves crafted
responses — one healthy scenario in which every check must pass, and one scenario per check in which
a single deliberate defect is planted and *that* check must go red.

This is the same reasoning as ``tests/test_kill_test_falsifiability.py`` applied to a much smaller
gate. It needs no database, no Docker, no cloud and no credentials, which is what makes it runnable
on every build.

**Standard library only**, so it runs wherever ``smoke.py`` runs.
"""

from __future__ import annotations

import dataclasses
import http.server
import json
import pathlib
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import smoke


@dataclasses.dataclass(frozen=True, slots=True)
class Behaviour:
    """How the stub should answer. Every field is a way a deployment could be wrong."""

    #: Liveness status field. Anything but ``alive`` is a defect.
    liveness_status: str = "alive"
    #: Whether liveness answers 200 at all.
    liveness_code: int = 200
    #: Whether the correlation header comes back on the response.
    echo_correlation: bool = True
    #: Whether a malformed inbound id is reflected verbatim instead of replaced.
    reflect_hostile_id: bool = False
    #: Readiness HTTP status.
    readiness_code: int = 200
    #: Readiness ``status`` field.
    readiness_status: str = "ready"
    #: Whether the readiness payload leaks a connection string.
    leak_dsn: bool = False
    #: What the queue answers to an anonymous caller.
    anonymous_queue_code: int = 401
    #: Whether the 401 carries the scheme hint.
    send_www_authenticate: bool = True
    #: Whether ``/docs`` is served.
    serve_docs: bool = False


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves the shapes ``smoke.py`` reads, under the behaviour bound to the server."""

    behaviour: Behaviour = Behaviour()
    correlation_header: str = "X-Request-ID"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        """Silent. The battery's own output is the only thing worth reading."""

    def _respond(self, code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _correlation_headers(self) -> dict[str, str]:
        behaviour = self.behaviour
        if not behaviour.echo_correlation:
            return {}
        supplied = self.headers.get(self.correlation_header)
        if supplied is None:
            return {self.correlation_header: "generated-id"}
        trusted = smoke.CORRELATION_ID_PATTERN.match(supplied) is not None
        if trusted or behaviour.reflect_hostile_id:
            return {self.correlation_header: supplied}
        return {self.correlation_header: "generated-id"}

    def do_GET(self) -> None:
        behaviour = self.behaviour
        path = self.path.split("?", 1)[0]

        if path == smoke.LIVENESS_PATH:
            self._respond(
                behaviour.liveness_code,
                {
                    "status": behaviour.liveness_status,
                    "service": "ledger-exception-control-plane",
                    "version": "0.1.0",
                },
                self._correlation_headers(),
            )
            return

        if path == smoke.READINESS_PATH:
            dependencies = [
                {"name": "postgres", "status": "healthy"},
                {"name": "redis", "status": "healthy"},
            ]
            if behaviour.leak_dsn:
                dependencies[0]["dsn"] = "postgresql://lecp:hunter2@db.example:5432/lecp"
            self._respond(
                behaviour.readiness_code,
                {"status": behaviour.readiness_status, "dependencies": dependencies},
            )
            return

        if path == smoke.QUEUE_PATH:
            authorised = (self.headers.get("Authorization") or "").startswith("Bearer ")
            if not authorised:
                headers = {"WWW-Authenticate": "Bearer"} if behaviour.send_www_authenticate else {}
                self._respond(
                    behaviour.anonymous_queue_code,
                    {"detail": "an authenticated principal is required"},
                    headers,
                )
                return
            self._respond(
                200,
                [
                    {
                        "id": "00000000-0000-0000-0000-000000000001",
                        "classification": "FEE_VARIANCE",
                        "status": "OPEN",
                        "psp_reference": "psp-1",
                        "currency": "EUR",
                        "amount": "1.23",
                        "correlation_id": "corr-1",
                        "has_proposal": False,
                        "decided": False,
                    }
                ],
            )
            return

        if path == smoke.DOCS_PATH:
            if behaviour.serve_docs:
                self._respond(200, {"docs": "swagger"})
            else:
                self._respond(404, {"detail": "Not Found"})
            return

        self._respond(404, {"detail": "Not Found"})


@contextmanager
def stub(behaviour: Behaviour) -> Iterator[str]:
    """Serve ``behaviour`` on an ephemeral loopback port for the duration of the block."""
    handler = type("_Bound", (_Handler,), {"behaviour": behaviour})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def outcomes(base_url: str, *, environment: str = "production") -> dict[str, smoke.CheckResult]:
    """Run every check against the stub and return the results by name.

    ``production`` by default so the documentation-suppression check is in scope; a token is
    supplied so the authenticated read is exercised rather than skipped.
    """
    target = smoke.Target(
        base_url=base_url,
        environment=environment,
        timeout=5.0,
        correlation_header="X-Request-ID",
        token="a-stub-token",
    )
    results = smoke.run_checks(target, require_ready=True, require_queue_read=True)
    return {result.name: result for result in results}


#: One planted defect per check: the behaviour to serve, and the check that must go red for it.
#: A row that fails means the check named on the right has stopped being able to detect the
#: deployment fault on the left.
MUTATIONS: Final[tuple[tuple[str, Behaviour, str], ...]] = (
    ("liveness answers 503", Behaviour(liveness_code=503), "liveness"),
    ("liveness reports the wrong status", Behaviour(liveness_status="ok"), "liveness"),
    ("the correlation header is dropped", Behaviour(echo_correlation=False), "correlation-id-echo"),
    (
        "a malformed correlation id is reflected",
        Behaviour(reflect_hostile_id=True),
        "correlation-id-sanitised",
    ),
    ("readiness answers 503", Behaviour(readiness_code=503), "readiness"),
    ("readiness claims ready while degraded", Behaviour(readiness_status="not_ready"), "readiness"),
    ("the readiness body leaks a DSN", Behaviour(leak_dsn=True), "readiness"),
    (
        "the queue is world-readable",
        Behaviour(anonymous_queue_code=200),
        "queue-refuses-anonymous",
    ),
    (
        "the 401 omits the scheme hint",
        Behaviour(send_www_authenticate=False),
        "queue-refuses-anonymous",
    ),
    ("production serves interactive docs", Behaviour(serve_docs=True), "docs-suppressed"),
)


def main() -> int:
    failures: list[str] = []

    with stub(Behaviour()) as base_url:
        healthy = outcomes(base_url)
    for name, result in sorted(healthy.items()):
        if result.outcome is not smoke.Outcome.PASS:
            failures.append(f"healthy stub: {name} was {result.outcome} ({result.detail})")
    print(
        f"  healthy stub: {len(healthy)} checks, "
        f"{sum(1 for r in healthy.values() if r.outcome is smoke.Outcome.PASS)} passed"
    )

    for description, behaviour, expected in MUTATIONS:
        with stub(behaviour) as base_url:
            results = outcomes(base_url)
        result = results.get(expected)
        if result is None:
            failures.append(f"{description}: there is no check named {expected!r}")
            continue
        if result.outcome is not smoke.Outcome.FAIL:
            failures.append(
                f"{description}: {expected} stayed {result.outcome} — the check cannot detect it"
            )
            print(f"  MISS  {expected:<26}  {description}")
            continue
        print(f"  CAUGHT  {expected:<24}  {description} -> {result.detail}")

    if failures:
        print("\nselftest: FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"\nselftest: OK ({len(MUTATIONS)} planted defects, all detected)")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
