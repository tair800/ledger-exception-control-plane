"""The configuration path for self-hosted Langfuse. §18, and §17 for how the keys are handled.

Langfuse accepts OpenTelemetry over OTLP/HTTP, so nothing here is a Langfuse client: this module
turns three environment variables into an OTLP endpoint and an authorisation header, and the
OpenTelemetry exporter does the rest. That is the whole integration, and keeping it that size is the
point — a vendor SDK in the instrumentation layer is a vendor SDK every later project inherits, and
§18's conventions are meant to be portable to whichever backend a project uses.

**Self-hosted, per the blueprint.** No third-party account is required to run this stack (NFR-7), so
the host is a Compose service and the keys are the ones that deployment issues.
``docs/observability.md`` carries the service block.

**Only variable NAMES appear in this file, and only names appear in the documentation.** The secret
key is a credential: it is read from the environment, wrapped in ``SecretStr`` so a ``repr``, a
``model_dump`` or a validation error renders asterisks, and it is never logged, never put on a span
and never returned as a plain string. The one function that must produce the header value returns a
``SecretStr`` too, so unwrapping it is an explicit, greppable act at the call site that hands it to
the exporter.

**Deliberately not a ``LECP_``-prefixed setting.** ``Settings`` declares ``env_prefix="LECP_"`` with
``extra="forbid"``, so any ``LECP_``-named variable that is not a declared field makes startup fail
— which means documenting one in ``.env.example`` would break the app for anybody who copied it into
``.env``. The cassette harness hit exactly this and named its switch ``CASSETTE_CAPTURE`` for the
same reason. The names below are the vendor's own and OpenTelemetry's own, which is also what makes
them recognisable to an operator who has configured either before.
"""

from __future__ import annotations

import base64
import dataclasses
import os
from collections.abc import Mapping
from typing import Final

from pydantic import SecretStr

__all__ = [
    "ENVIRONMENT_VARIABLES",
    "LANGFUSE_HOST",
    "LANGFUSE_OTEL_PATH",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS",
    "OTEL_SDK_DISABLED",
    "OTEL_SERVICE_NAME",
    "LangfuseConfigurationError",
    "LangfuseExportTarget",
    "langfuse_authorization_header",
    "langfuse_otlp_endpoint",
    "langfuse_target_from_environment",
]

#: Where the self-hosted instance is reachable, e.g. ``http://langfuse:3000`` inside Compose.
LANGFUSE_HOST: Final = "LANGFUSE_HOST"

#: The project's public key. Not a secret, and still not logged — pairing it with a trace would let
#: a reader identify the project a span belongs to from the log alone.
LANGFUSE_PUBLIC_KEY: Final = "LANGFUSE_PUBLIC_KEY"

#: The project's secret key. A credential.
LANGFUSE_SECRET_KEY: Final = "LANGFUSE_SECRET_KEY"

#: OpenTelemetry's own variables, honoured by the exporter rather than by this module. Named here so
#: the documentation has one list and an operator does not have to find them in two places.
OTEL_EXPORTER_OTLP_ENDPOINT: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
OTEL_EXPORTER_OTLP_HEADERS: Final = "OTEL_EXPORTER_OTLP_HEADERS"
OTEL_SERVICE_NAME: Final = "OTEL_SERVICE_NAME"
OTEL_SDK_DISABLED: Final = "OTEL_SDK_DISABLED"

#: Every variable this integration reads or documents. Names only.
ENVIRONMENT_VARIABLES: Final[tuple[str, ...]] = (
    LANGFUSE_HOST,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
    OTEL_EXPORTER_OTLP_ENDPOINT,
    OTEL_EXPORTER_OTLP_HEADERS,
    OTEL_SERVICE_NAME,
    OTEL_SDK_DISABLED,
)

#: Langfuse's OTLP ingestion path, appended to the host.
LANGFUSE_OTEL_PATH: Final = "/api/public/otel"


class LangfuseConfigurationError(ValueError):
    """The configured host cannot be used as an OTLP endpoint."""


def langfuse_otlp_endpoint(host: str) -> str:
    """The OTLP endpoint for a Langfuse host.

    Refuses a host with credentials embedded in it. A URL of the form
    ``https://user:key@langfuse.example`` would put a secret into the exporter's endpoint — which is
    logged by most exporters on startup, ends up in a container's environment dump, and is precisely
    the shape :func:`~ledger_exception_control_plane.observability.redaction.redact_text` exists to
    catch after the fact. Refusing it at configuration time is better than redacting it later.
    """
    trimmed = host.strip().rstrip("/")
    if not trimmed.startswith(("http://", "https://")):
        raise LangfuseConfigurationError(
            f"{LANGFUSE_HOST} must be an http:// or https:// URL; got {trimmed[:16]!r}"
        )
    if "@" in trimmed:
        raise LangfuseConfigurationError(
            f"{LANGFUSE_HOST} must not embed credentials. Supply the keys through "
            f"{LANGFUSE_PUBLIC_KEY} and {LANGFUSE_SECRET_KEY}."
        )
    return f"{trimmed}{LANGFUSE_OTEL_PATH}"


def langfuse_authorization_header(public_key: str, secret_key: SecretStr) -> SecretStr:
    """The ``Authorization`` header value Langfuse's OTLP endpoint expects.

    HTTP Basic over the key pair, which is what the vendor documents. Returned as a ``SecretStr``:
    base64 is an encoding, not encryption, so the header value is exactly as sensitive as the secret
    key inside it and must be treated the same everywhere it travels.
    """
    pair = f"{public_key}:{secret_key.get_secret_value()}".encode()
    return SecretStr(f"Basic {base64.b64encode(pair).decode('ascii')}")


@dataclasses.dataclass(frozen=True, slots=True)
class LangfuseExportTarget:
    """Everything the OTLP exporter needs, with the credential still wrapped.

    ``dataclasses`` renders ``SecretStr`` through its own ``repr``, which is asterisks — so this
    object can be logged, put in an error message, or included in a startup line without leaking the
    header. Unwrapping happens once, at the exporter construction site.
    """

    endpoint: str
    authorization: SecretStr


def langfuse_target_from_environment(
    environ: Mapping[str, str] | None = None,
) -> LangfuseExportTarget | None:
    """Build the export target from the environment, or ``None`` if it is not configured.

    ``None`` rather than an exception for a missing variable: telemetry is not configured in most
    environments this runs in — the test suite, a developer's machine, CI — and an unconfigured
    exporter must be a no-op rather than a startup failure. A *malformed* host is different and does
    raise, because that is a mistake somebody made rather than a decision not to export.

    The mapping is injectable so this is testable without touching the process environment; that is
    also why nothing here reads ``os.environ`` at import time.
    """
    source = environ if environ is not None else os.environ
    host = source.get(LANGFUSE_HOST)
    public_key = source.get(LANGFUSE_PUBLIC_KEY)
    secret_key = source.get(LANGFUSE_SECRET_KEY)
    if not host or not public_key or not secret_key:
        return None
    return LangfuseExportTarget(
        endpoint=langfuse_otlp_endpoint(host),
        authorization=langfuse_authorization_header(public_key, SecretStr(secret_key)),
    )
