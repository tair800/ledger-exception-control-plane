"""Shared fixtures.

Unit tests never touch Docker. Dependency behaviour is simulated at the probe boundary —
the seam ``run_probe`` already exposes — so failure and timeout cases are deterministic and
run in milliseconds. The real stack is exercised separately by the ``integration`` marker.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from ledger_exception_control_plane.config import Settings

#: A password that must never appear in a response, a log line or an error. Tests assert on
#: this exact string, so a leak anywhere fails the suite loudly.
SECRET_PASSWORD = "super-secret-password-do-not-leak"


@pytest.fixture
def settings() -> Settings:
    """Settings whose DSNs embed a recognisable secret, for leak detection."""
    return Settings(
        environment="ci",
        log_level="INFO",
        postgres_dsn=SecretStr(f"postgresql://lecp:{SECRET_PASSWORD}@db:5432/lecp"),
        redis_dsn=SecretStr(f"redis://:{SECRET_PASSWORD}@cache:6379/0"),
        readiness_timeout_seconds=0.2,
    )
