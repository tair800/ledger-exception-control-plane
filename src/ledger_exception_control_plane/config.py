"""Typed, environment-driven application configuration.

Two rules govern this module and are enforced by tests:

* **Secrets never leak.** Connection strings carry credentials, so they are held as
  ``SecretStr``. Pydantic renders those as ``**********`` in ``repr``, in ``model_dump()``
  and in validation errors, so a stray log line or a traceback cannot expose a password.
  Reading the real value requires an explicit ``.get_secret_value()`` call, which is easy
  to grep for and appears in exactly one place per dependency.
* **Invalid required configuration fails loudly at startup**, not on the first request.

Defaults exist only where a local-development value is safe and obvious. They point at the
Compose stack on localhost and use development-only placeholder credentials.
"""

from __future__ import annotations

import re
from typing import Final, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# A correlation id arriving from outside is untrusted input that ends up in log records.
# Constraining it to a short, boring alphabet is what stops a header becoming a
# log-injection payload: no newlines, no control characters, no unbounded length.
CORRELATION_ID_PATTERN: Final = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
CORRELATION_ID_MAX_LENGTH: Final = 128


class Settings(BaseSettings):
    """Application settings, populated from the environment or a local ``.env`` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LECP_",
        extra="forbid",
        frozen=True,
    )

    service_name: str = "ledger-exception-control-plane"
    environment: Literal["local", "ci", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Development-only placeholder credentials. They are scoped to the local Compose stack,
    # which binds to localhost and holds no real data. Any deployed environment must supply
    # its own values through the environment; nothing here is a production secret.
    #
    # The ports are the stack's published host ports (15432 / 16379), NOT the service
    # defaults. This matters: a developer machine very often runs its own PostgreSQL on 5432,
    # and a default pointing there means an unconfigured `alembic upgrade head` runs DDL
    # against an unrelated database that happens to accept the credentials. The default must
    # be wrong-but-harmless (connection refused), never right-for-the-wrong-database.
    postgres_dsn: SecretStr = SecretStr("postgresql://lecp:lecp_local_dev@localhost:15432/lecp")
    redis_dsn: SecretStr = SecretStr("redis://localhost:16379/0")

    #: Upper bound on each individual readiness probe. Readiness must answer promptly even
    #: when a dependency is hanging rather than refusing connections — an unbounded probe
    #: turns a slow dependency into a slow health endpoint and then into a false outage.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)

    #: Header carrying an inbound correlation id. Documented in the README.
    correlation_id_header: str = "X-Request-ID"

    # -- Bounded retry (M4.3, §15) ------------------------------------------------------
    #
    # `PROJECT_SPEC.md` §15 says "base delay, multiplier and cap are configuration" and gives no
    # values, and no ADR, migration or .env file in this repository names one either. **These
    # defaults are therefore declared project decisions, not empirical findings** (ADR-055), chosen
    # to be conservative for a financial counterparty rather than tuned against a measurement that
    # does not exist. Every one is overridable, and the bounds below reject the values that would
    # turn the policy into something other than a bounded retry.

    #: First delay before a re-send, in seconds. One second: long enough that a counterparty
    #: restarting is not hit immediately, short enough that a transient blip clears within a pass.
    retry_base_delay_seconds: float = Field(default=1.0, gt=0.0, le=3600.0)

    #: Growth factor per attempt. Two is the conventional doubling; below 1.0 the curve would
    #: shrink, which the field bound rejects rather than leaving to a runtime surprise.
    retry_multiplier: float = Field(default=2.0, ge=1.0, le=10.0)

    #: Ceiling on the computed delay, in seconds. Sixty seconds keeps the longest gap inside a
    #: normal operational attention span; an unbounded curve would put later attempts hours apart
    #: while still counting as "retrying".
    retry_cap_seconds: float = Field(default=60.0, gt=0.0, le=86400.0)

    #: Maximum sends per operation, the first included. Five is small on purpose: this bound exists
    #: so a failing dispatch reaches a human, and a large ceiling defers that without improving the
    #: odds — an endpoint that refused five connections over the backoff curve is down, not busy.
    retry_max_attempts: int = Field(default=5, ge=1, le=100)

    #: Wall-clock budget for the whole operation, in seconds, measured from the first send. §15
    #: requires this second, independent bound "so an entry cannot retry indefinitely". One hour:
    #: past that, a settlement adjustment waiting on a dead endpoint is an operational problem, not
    #: a transient one.
    retry_time_budget_seconds: float = Field(default=3600.0, gt=0.0, le=604800.0)

    @field_validator("postgres_dsn", "redis_dsn")
    @classmethod
    def _reject_empty_dsn(cls, value: SecretStr) -> SecretStr:
        """Fail at startup on a blank DSN rather than at the first readiness probe.

        The error message deliberately names the field but never echoes the value.
        """
        if not value.get_secret_value().strip():
            raise ValueError("must not be empty")
        return value


def is_valid_correlation_id(candidate: str) -> bool:
    """Return whether an externally supplied correlation id may be trusted verbatim.

    Anything failing this check is replaced with a generated id rather than rejected with
    an error: a malformed header is not worth failing a request over, but it must never
    reach a log record unmodified.
    """
    return bool(CORRELATION_ID_PATTERN.match(candidate))
