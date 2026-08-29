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
