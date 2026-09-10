"""Which commit a running instance was built from, and why that had to become observable.

Before this, `/api/v1/meta` reported `version: "0.1.0"` — the project's version, unchanged since
M0. Nothing a running instance served said which *build* it was. Checking that a deployment matched
the commit it was supposed to be meant opening a provider dashboard: fine for a person, useless to
a smoke test, and impossible to assert in CI.

The revision is read from the platform's own environment rather than baked into the image, so it
cannot drift from what the platform actually deployed.

No database, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ledger_exception_control_plane.api import create_app
from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.routes import _REVISION_VARIABLES, deployed_revision

SHA = "1b8336d181d8aed02f4b6a9339e30ec0c4f6edda"


def _client() -> TestClient:
    return TestClient(create_app(Settings(postgres_dsn=SecretStr("postgresql://u:p@h/db"))))


@pytest.mark.parametrize("variable", _REVISION_VARIABLES)
def test_each_supported_platform_variable_is_read(variable: str) -> None:
    """Render, Vercel and a generic fallback. Parametrised over the constant, not over a copy.

    A hard-coded list here would let the tuple gain a name that nothing tested — which is the usual
    way a "supported platform" turns out not to be.
    """
    assert deployed_revision({variable: SHA}) == SHA


def test_the_first_variable_set_wins_in_declared_order() -> None:
    """Deterministic when more than one is present, rather than dictionary-order.

    A container can carry several: a Render service built from a repository that also has Vercel
    integration sees both. Order is the tuple's, so the answer is a property of the code.
    """
    everything = dict.fromkeys(_REVISION_VARIABLES, "later")
    everything[_REVISION_VARIABLES[0]] = "first"
    assert deployed_revision(everything) == "first"


def test_an_unset_platform_reports_nothing_rather_than_guessing() -> None:
    """**Empty, never invented.**

    A local run, a test client and a container started by hand all have no build commit. Reporting
    a plausible-looking value — the working tree's HEAD, say — would be a claim about a deployment
    that is not true, and the one place it would be believed is a smoke test comparing it to an
    expected SHA.
    """
    assert deployed_revision({}) == ""
    assert deployed_revision({"RENDER_GIT_COMMIT": "   "}) == "", "whitespace is not a revision"


def test_meta_reports_the_revision_and_stays_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint a smoke test reads. No token, and nothing sensitive."""
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    body = _client().get("/api/v1/meta").json()

    assert body["revision"] == SHA
    assert set(body) == {"version", "demo_mode", "ledger_adapter", "revision"}


def test_meta_reports_an_empty_revision_when_the_platform_sets_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in _REVISION_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    assert _client().get("/api/v1/meta").json()["revision"] == ""


def test_the_revision_is_not_treated_as_configuration() -> None:
    """It is read from the environment directly, and deliberately not through ``Settings``.

    `Settings` is `extra="forbid"` over the `LECP_` namespace, so a platform variable would have to
    be declared there to be readable — which would mean this project enumerating every host it
    might ever run on, and a new platform becoming a settings change. These are facts the host
    states about the build, not choices this project makes.
    """
    assert not any(name.startswith("LECP_") for name in _REVISION_VARIABLES)
    assert all(name not in Settings.model_fields for name in _REVISION_VARIABLES)


def test_the_revision_is_a_public_fact_and_not_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Worth stating, because `/api/v1/meta` is unauthenticated.

    A commit hash from a public repository reveals nothing that reading the repository would not.
    The endpoint's own docstring commits to carrying nothing sensitive, and this pins that the
    addition did not quietly break that promise: no dependency, origin, principal or configuration
    value appears beside it.
    """
    monkeypatch.setenv("RENDER_GIT_COMMIT", SHA)
    body = _client().get("/api/v1/meta").json()

    rendered = str(body).lower()
    for forbidden in ("postgres", "redis", "dsn", "password", "token", "principal", "origin"):
        assert forbidden not in rendered
