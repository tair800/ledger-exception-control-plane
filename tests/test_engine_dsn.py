"""The DSN a managed PostgreSQL provider hands you, and what asyncpg will actually accept.

**This module exists because of a failure mode that reports itself as healthy.**

`asyncpg.connect(dsn)` parses a URL itself and understands libpq's `sslmode`. SQLAlchemy's asyncpg
dialect does something different: it splits the query string into keyword arguments and calls
`asyncpg.connect(**kwargs)` — and that signature has no `**kwargs` catch-all, so `sslmode` and
`channel_binding` raise `TypeError`.

The readiness probe uses the first path. Every query the application makes uses the second. Paste a
Neon connection string in verbatim and `/readyz` goes **green** while nothing else works at all.

No database and no network: every test here is about string handling and the driver's declared
signature, which is exactly why it can run in the ordinary suite and catch this before a deploy.
"""

from __future__ import annotations

import inspect

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import make_url

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn

#: A Neon connection string's shape. Host and credentials are invented; the query string is the
#: part under test and is what Neon actually issues.
NEON_DIRECT = (
    "postgresql://demo:secret@ep-demo-a1b2c3.eu-central-1.aws.neon.tech/lecp_demo"
    "?sslmode=require&channel_binding=require"
)
NEON_POOLED = (
    "postgresql://demo:secret@ep-demo-a1b2c3-pooler.eu-central-1.aws.neon.tech/lecp_demo"
    "?sslmode=require&channel_binding=require"
)
LOCAL = "postgresql://lecp:lecp_local_dev@localhost:15432/lecp"


def _kwargs(dsn: str) -> dict[str, object]:
    """What SQLAlchemy's asyncpg dialect would pass to ``asyncpg.connect``.

    Untyped in SQLAlchemy's stubs, so the two calls are ignored explicitly rather than the module
    being excluded from strict checking — the point of this file is the *shape* of those keywords,
    and asking the real dialect is the only way to learn it that cannot drift from what ships.
    """
    dialect = postgresql.asyncpg.dialect()  # type: ignore[no-untyped-call]
    _, keywords = dialect.create_connect_args(make_url(dsn))  # type: ignore[no-untyped-call]
    return dict(keywords)


def _normalised(raw: str) -> str:
    return async_dsn(Settings(postgres_dsn=SecretStr(raw)))


def test_asyncpg_really_has_no_keyword_catch_all() -> None:
    """**The premise of this whole module, asserted rather than assumed.**

    If a future asyncpg grew `**kwargs`, or started accepting `sslmode`, the normalisation below
    would be unnecessary — and someone should find that out from a failing test rather than by
    reading this file and wondering.
    """
    parameters = inspect.signature(asyncpg.connect).parameters
    assert not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())
    assert "sslmode" not in parameters
    assert "channel_binding" not in parameters
    # And the two it does accept, which the normalisation maps onto.
    assert "ssl" in parameters
    assert "statement_cache_size" in parameters


@pytest.mark.parametrize("raw", [NEON_DIRECT, NEON_POOLED], ids=["direct", "pooled"])
def test_a_managed_providers_dsn_produces_no_keyword_asyncpg_would_reject(raw: str) -> None:
    """The regression test. Verbatim, these DSNs break every query and no health check."""
    keywords = _kwargs(_normalised(raw))

    assert "sslmode" not in keywords
    assert "channel_binding" not in keywords
    assert keywords["ssl"] == "require", "TLS must survive the translation, not be dropped with it"


def test_the_unmodified_dsn_would_have_broken_and_that_is_why_this_exists() -> None:
    """The failing case, proven rather than described.

    Without normalisation the dialect hands asyncpg two keywords it has no parameters for. Asserted
    here so the fix cannot be deleted as unnecessary by someone who reads only the passing tests.
    """
    straight_through = "postgresql+asyncpg://" + NEON_DIRECT.split("://", 1)[1]
    keywords = _kwargs(straight_through)

    assert keywords["sslmode"] == "require"
    assert keywords["channel_binding"] == "require"
    accepted = inspect.signature(asyncpg.connect).parameters
    assert {"sslmode", "channel_binding"} - set(accepted) == {"sslmode", "channel_binding"}


def test_a_pooled_endpoint_disables_the_prepared_statement_cache() -> None:
    """pgBouncer in transaction mode does not keep a prepared statement across checkouts.

    asyncpg caches them by default, so the second query on a recycled connection fails with a
    statement that no longer exists. Neon marks pooled hosts `-pooler`, which is the only signal
    available in the DSN.
    """
    assert _kwargs(_normalised(NEON_POOLED))["prepared_statement_cache_size"] == 0


def test_a_direct_endpoint_keeps_the_cache() -> None:
    """The cache is a real performance feature; it is disabled only where it breaks."""
    assert "prepared_statement_cache_size" not in _kwargs(_normalised(NEON_DIRECT))


def test_a_local_dsn_is_unchanged_apart_from_the_driver() -> None:
    """The development DSN carries no query string and must come back untouched."""
    assert _normalised(LOCAL) == "postgresql+asyncpg://lecp:lecp_local_dev@localhost:15432/lecp"


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full", "prefer", "allow"])
def test_every_tls_asking_sslmode_becomes_ssl_require(mode: str) -> None:
    """asyncpg's `ssl` is not libpq's `sslmode` and has no verify-ca/verify-full equivalent here.

    Mapping them all to `require` is a deliberate narrowing and is stated as one: the connection is
    encrypted, and certificate verification beyond that is asyncpg's default behaviour for an
    `ssl=require` connection rather than something this DSN can ask for.
    """
    keywords = _kwargs(_normalised(f"postgresql://u:p@h.example/db?sslmode={mode}"))
    assert keywords["ssl"] == "require"


def test_sslmode_disable_is_dropped_rather_than_translated() -> None:
    """`ssl=disable` is not a value asyncpg defines. No TLS is its default, so say nothing."""
    keywords = _kwargs(_normalised("postgresql://u:p@h.example/db?sslmode=disable"))
    assert "ssl" not in keywords
    assert "sslmode" not in keywords


def test_an_unrecognised_parameter_is_kept_rather_than_silently_dropped() -> None:
    """**The normalisation knows about two parameters and must not pretend to know about more.**

    Dropping everything unfamiliar would turn a future provider's required option into a connection
    that quietly ignores it — a worse failure than the one this module fixes, because there would be
    no error at all.
    """
    keywords = _kwargs(_normalised("postgresql://u:p@h.example/db?application_name=lecp"))
    assert keywords["application_name"] == "lecp"


def test_a_dsn_with_no_recognised_scheme_is_refused() -> None:
    with pytest.raises(ValueError, match="postgresql://"):
        _normalised("mysql://u:p@h/db")


def test_the_password_does_not_survive_into_a_repr() -> None:
    """The DSN carries a password and this function is the one place it is unwrapped."""
    settings = Settings(postgres_dsn=SecretStr(NEON_DIRECT))
    assert "secret" not in repr(settings)
    assert "secret" not in str(settings.postgres_dsn)
