"""Configuration validation.

Two requirements are pinned here: invalid required configuration fails at startup rather
than at the first request, and a validation error never echoes the value it rejected.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ledger_exception_control_plane.config import (
    CORRELATION_ID_MAX_LENGTH,
    Settings,
    is_valid_correlation_id,
)
from tests.conftest import SECRET_PASSWORD


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_dsn_is_rejected_at_construction(blank: str) -> None:
    """A blank DSN must fail immediately, not on the first readiness probe."""
    with pytest.raises(ValidationError) as error:
        Settings(postgres_dsn=SecretStr(blank))

    assert "postgres_dsn" in str(error.value)


def test_validation_error_does_not_echo_the_rejected_secret() -> None:
    """A rejected DSN must not appear in the error text, which commonly reaches logs."""
    with pytest.raises(ValidationError) as error:
        Settings(redis_dsn=SecretStr(""), readiness_timeout_seconds=-1)

    rendered = str(error.value)
    assert "readiness_timeout_seconds" in rendered
    assert SECRET_PASSWORD not in rendered


@pytest.mark.parametrize("invalid_timeout", [0.0, -1.0, 31.0])
def test_readiness_timeout_must_be_bounded(invalid_timeout: float) -> None:
    """An unbounded or absent timeout would defeat the point of a bounded probe."""
    with pytest.raises(ValidationError):
        Settings(readiness_timeout_seconds=invalid_timeout)


def test_unknown_setting_is_rejected() -> None:
    """``extra="forbid"`` turns a typo in an environment variable into a startup failure."""
    with pytest.raises(ValidationError):
        Settings(postgres_dsnn=SecretStr("postgresql://x"))  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "candidate",
    ["abc", "A1_b-2", "0" * CORRELATION_ID_MAX_LENGTH, "req-01HZX9K4T7QP2"],
)
def test_correlation_id_policy_accepts_safe_values(candidate: str) -> None:
    assert is_valid_correlation_id(candidate) is True


@pytest.mark.parametrize(
    "candidate",
    [
        "",
        "0" * (CORRELATION_ID_MAX_LENGTH + 1),
        "has space",
        "new\nline",
        "carriage\rreturn",
        "semi;colon",
        'quote"brace}',
        # U+2028 LINE SEPARATOR. Ruff flags it as an ambiguous character, which is exactly
        # why it belongs here: several JSON and log-viewer stacks treat it as a line break,
        # so it is a real injection vector that a naive "no newline" check would miss.
        "unicode separator",  # noqa: RUF001
    ],
)
def test_correlation_id_policy_rejects_unsafe_values(candidate: str) -> None:
    """Every rejected shape is a way a header could corrupt a log stream."""
    assert is_valid_correlation_id(candidate) is False


def test_every_setting_is_documented_in_the_env_example() -> None:
    """**The file a deployment copies must list every knob the application reads.**

    Nothing enforced this before M4.3, and M4.3 immediately demonstrated why: five new
    ``LECP_RETRY_*`` settings shipped with the application reading them and ``.env.example``
    silently not mentioning them, so a deployment copying the example got the declared defaults
    with no visible control over the two bounds that decide how many times an irreversible
    financial write is offered to a ledger. A reviewer found it; this is what would have.

    The check runs the other way too. A variable documented here that ``Settings`` does not accept
    is worse than an undocumented one, because ``extra="forbid"`` makes it fail at startup — the
    example would be handing out a file that cannot be used.
    """
    import pathlib

    from ledger_exception_control_plane.config import Settings

    example = (pathlib.Path(__file__).resolve().parents[1] / ".env.example").read_text(
        encoding="utf-8"
    )
    documented = {
        line.split("=", 1)[0].strip()
        for line in example.splitlines()
        if line.strip().startswith("LECP_") and "=" in line
    }
    expected = {f"LECP_{name.upper()}" for name in Settings.model_fields}

    assert expected - documented == set(), (
        f"settings the application reads but .env.example never mentions: "
        f"{sorted(expected - documented)}"
    )
    assert documented - expected == set(), (
        f".env.example documents variables Settings would reject at startup: "
        f"{sorted(documented - expected)}"
    )
