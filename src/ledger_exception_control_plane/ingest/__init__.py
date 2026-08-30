"""Deterministic settlement ingestion — increment M2.1.

Raw bytes in, either normalised settlement lines or a quarantined batch out. The boundary that
makes an untrusted file safe to build on.

**This package performs no reconciliation.** It does not look up a ledger entry, compare two
amounts, evaluate a tolerance, decide that anything matched, classify a residual or create an
exception. Those are M2.2 onward, and a test walks this package's AST to keep it that way — a
"temporary matcher" written here to validate normalisation would make every later test that
consumes the result circular.

Three responsibilities, kept apart on purpose:

* :mod:`.parser` — the wire format and nothing else. Text in, text out, structural defects.
* :mod:`.normalise` — text to typed canonical values, with the money and currency rules.
* :mod:`.service` — receipt before parse, then one atomic outcome.

Callable without HTTP::

    from ledger_exception_control_plane.ingest import ingest

    outcome = await ingest(engine, payload, source="file-drop", received_at=arrived)
"""

from ledger_exception_control_plane.ingest.errors import (
    MAX_REASON_LENGTH,
    Defect,
    QuarantineCode,
    render_reason,
)
from ledger_exception_control_plane.ingest.normalise import NormalisedLine, normalise
from ledger_exception_control_plane.ingest.parser import (
    SETTLEMENT_COLUMNS,
    ParsedRow,
    ParseResult,
    parse,
)
from ledger_exception_control_plane.ingest.service import (
    IngestOutcome,
    content_hash,
    ingest,
    interpret,
)

__all__ = [
    "MAX_REASON_LENGTH",
    "SETTLEMENT_COLUMNS",
    "Defect",
    "IngestOutcome",
    "NormalisedLine",
    "ParseResult",
    "ParsedRow",
    "QuarantineCode",
    "content_hash",
    "ingest",
    "interpret",
    "normalise",
    "parse",
    "render_reason",
]
