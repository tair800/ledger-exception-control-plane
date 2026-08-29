"""Byte-stable rendering. Every choice here exists to make regeneration reproduce bytes.

The plan's exit criterion is that the corpus regenerates *byte-identically*, which is stricter
than NFR-9's "same seed, same corpus" and is the reason a drift check can be a simple file
comparison rather than a semantic diff. Four things would otherwise vary between machines and
runs, and each is pinned explicitly:

* **Line endings.** ``\\n`` everywhere, written in binary. Python's text mode would translate
  to ``\\r\\n`` on Windows, so a corpus generated there would differ from one generated in CI
  for no reason anyone would enjoy debugging.
* **Encoding.** UTF-8, no BOM, stated rather than inherited from the locale.
* **Ordering.** Rows are ordered by the generator; JSON keys are sorted.
* **Number formatting.** Amounts are rendered from ``Decimal`` by ``str``, so a value keeps the
  scale its currency actually uses. No float ever touches this path.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any, Final

from pydantic import BaseModel

from ledger_exception_control_plane.fixtures.catalogue import BuiltEntry, BuiltLine
from ledger_exception_control_plane.fixtures.money import render

SETTLEMENT_COLUMNS: Final[tuple[str, ...]] = (
    "psp_reference",
    "merchant_reference",
    "transaction_type",
    "amount",
    "currency",
    "value_date",
    "presentment_amount",
    "presentment_currency",
    "fx_rate",
    "memo",
)

LEDGER_COLUMNS: Final[tuple[str, ...]] = (
    "external_ref",
    "account_code",
    "amount",
    "currency",
    "booked_at",
    "description",
)


def _to_csv(header: tuple[str, ...], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO(newline="")
    # QUOTE_MINIMAL with an explicit terminator: a memo containing a comma is quoted, which is
    # both realistic and deterministic, and nothing else is.
    writer = csv.writer(buffer, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def render_settlement_csv(lines: tuple[BuiltLine, ...]) -> bytes:
    """The raw PSP settlement file — the artifact M2.1 will parse.

    This is a *writer*, not the inverse of a parser that does not exist yet. It defines the
    file format (OPEN-1) and nothing more: no field here is computed from another, so nothing
    a future normaliser must decide is decided here.
    """
    rows = [
        [
            line.psp_reference,
            line.merchant_reference or "",
            line.transaction_type,
            render(line.amount),
            line.currency.code,
            line.value_date.isoformat(),
            render(line.presentment_amount) if line.presentment_amount is not None else "",
            line.presentment_currency.code if line.presentment_currency is not None else "",
            line.fx_rate or "",
            line.memo,
        ]
        for line in lines
    ]
    return _to_csv(SETTLEMENT_COLUMNS, rows)


def render_ledger_csv(entries: tuple[BuiltEntry, ...]) -> bytes:
    """The ledger snapshot — the other side matching needs in order to have a residual."""
    rows = [
        [
            entry.external_ref,
            entry.account_code,
            render(entry.amount),
            entry.currency.code,
            entry.booked_at.isoformat(),
            entry.description or "",
        ]
        for entry in entries
    ]
    return _to_csv(LEDGER_COLUMNS, rows)


def render_json(model: BaseModel) -> bytes:
    """Deterministic JSON: sorted keys, fixed indent, explicit newline, UTF-8.

    ``mode="json"`` renders ``Decimal`` as a *string*, which is the point — a JSON number
    would be a float on the way back in, and a monetary value that round-trips through binary
    floating point has already lost.
    """
    payload: Any = model.model_dump(mode="json")
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def content_digest(files: dict[str, bytes]) -> str:
    """SHA-256 over every artifact, in sorted path order.

    Paths are length-prefixed into the digest alongside their contents, so renaming a file
    changes the hash and no two different (path, content) splits can collide.
    """
    hasher = hashlib.sha256()
    for path in sorted(files):
        encoded = path.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
        hasher.update(len(files[path]).to_bytes(8, "big"))
        hasher.update(files[path])
    return hasher.hexdigest()
