"""Reading a corpus from disk and loading it into PostgreSQL.

Two safety properties matter more here than convenience, because this is the one part of the
fixture system that writes to a database.

**It refuses to run against anything that is not obviously disposable.** The target database
name must be on a short allowlist. A fixture loader that can be pointed at a database by
accident is a data-loss tool with a friendly name, and the developer machine this was built on
runs an unrelated PostgreSQL on the default port.

**It never disables a constraint.** Rows go in through the ORM against the real schema with
every check, foreign key and unique constraint enforced — which is the entire point of the
plan's "committed sample loads" criterion. A load that had to switch off integrity to succeed
would prove the corpus is *not* loadable.

Reset is by identifier, never by ``TRUNCATE``. Fixture rows carry deterministic UUIDv5
identifiers, so the loader can delete exactly the rows it owns and leave anything else in the
database untouched.
"""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import create_engine
from ledger_exception_control_plane.db.models import LedgerEntry, SettlementBatch, SettlementLine

# Re-exported for every existing caller. `as` form because mypy's strict re-export rule wants a
# module to say explicitly which names it passes on, and these are part of this module's published
# surface even though the rule they implement now lives elsewhere.
from ledger_exception_control_plane.disposable import (
    DISPOSABLE_DATABASE as DISPOSABLE_DATABASE,
)
from ledger_exception_control_plane.disposable import (
    UnsafeTargetError as UnsafeTargetError,
)
from ledger_exception_control_plane.disposable import (
    assert_target_is_disposable as assert_target_is_disposable,
)
from ledger_exception_control_plane.disposable import (
    database_name as database_name,
)
from ledger_exception_control_plane.fixtures.generator import (
    MANIFEST_PATH,
    RECORDS_PATH,
    SCENARIOS_PATH,
)
from ledger_exception_control_plane.fixtures.schema import (
    FIXTURE_SCHEMA_VERSION,
    Corpus,
    Manifest,
    ScenarioCatalogue,
)
from ledger_exception_control_plane.fixtures.serialise import content_digest

# The disposability rule lives in `disposable.py` and is re-exported here, because the demo
# reset endpoint needs the same guard and a production module may not import this package — see
# that module's docstring. Re-exported rather than moved-and-rewired so every existing caller and
# test keeps working against one implementation.


class CorpusIntegrityError(RuntimeError):
    """Raised when a corpus on disk does not match its own manifest."""


@dataclasses.dataclass(frozen=True, slots=True)
class LoadedCorpus:
    corpus: Corpus
    scenarios: ScenarioCatalogue
    manifest: Manifest
    root: Path

    def raw_payload(self, relative_path: str) -> bytes:
        return (self.root / relative_path).read_bytes()


def corpus_files(root: Path) -> dict[str, bytes]:
    """Every artifact under ``root``, keyed by POSIX-style relative path.

    Paths are normalised to forward slashes so a corpus generated on Windows and one generated
    on Linux hash identically — otherwise the drift check would fail on the separator alone.
    """
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def read_corpus(root: Path) -> LoadedCorpus:
    """Read and validate a corpus, including its own integrity claim.

    The manifest's digest is recomputed rather than trusted. A corpus whose files were edited
    by hand after generation is exactly the thing that would make a later test pass against
    data nobody can reproduce.
    """
    manifest = Manifest.model_validate_json((root / MANIFEST_PATH).read_bytes())
    if manifest.fixture_schema_version != FIXTURE_SCHEMA_VERSION:
        raise CorpusIntegrityError(
            f"corpus declares fixture schema {manifest.fixture_schema_version}; "
            f"this build reads {FIXTURE_SCHEMA_VERSION}"
        )

    files = corpus_files(root)
    files.pop(MANIFEST_PATH, None)
    actual = content_digest(files)
    if actual != manifest.content_sha256:
        raise CorpusIntegrityError(
            "corpus content does not match its manifest digest; regenerate rather than editing"
        )

    return LoadedCorpus(
        corpus=Corpus.model_validate_json((root / RECORDS_PATH).read_bytes()),
        scenarios=ScenarioCatalogue.model_validate_json((root / SCENARIOS_PATH).read_bytes()),
        manifest=manifest,
        root=root,
    )


async def load(loaded: LoadedCorpus, settings: Settings, *, reset: bool = False) -> dict[str, int]:
    """Insert the corpus, with every database constraint enforced.

    Returns the row counts written, so a caller can assert the load was complete rather than
    merely uneventful.
    """
    assert_target_is_disposable(settings)

    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session, session.begin():
            if reset:
                await _delete_owned_rows(session, loaded)

            for record in loaded.corpus.ledger_entries:
                session.add(
                    LedgerEntry(
                        id=record.id,
                        external_ref=record.external_ref,
                        account_code=record.account_code,
                        amount=record.amount,
                        currency=record.currency,
                        booked_at=record.booked_at,
                        description=record.description,
                    )
                )

            for batch in loaded.corpus.batches:
                payload = loaded.raw_payload(batch.raw_payload_path)
                # The corpus states the hash; the loader recomputes it. If the file were
                # edited after generation the batch would load with a hash that does not
                # describe its own payload, and FR-1's re-delivery guard would be built on it.
                digest = hashlib.sha256(payload).hexdigest()
                if digest != batch.content_hash:
                    raise CorpusIntegrityError(
                        f"{batch.raw_payload_path} does not hash to the recorded content_hash"
                    )
                session.add(
                    SettlementBatch(
                        id=batch.id,
                        content_hash=batch.content_hash,
                        source=batch.source,
                        raw_payload=payload,
                        received_at=batch.received_at,
                        status=batch.status,
                    )
                )
                for line in batch.lines:
                    session.add(
                        SettlementLine(
                            id=line.id,
                            settlement_batch_id=batch.id,
                            line_number=line.line_number,
                            psp_reference=line.psp_reference,
                            merchant_reference=line.merchant_reference,
                            transaction_type=line.transaction_type,
                            amount=line.amount,
                            currency=line.currency,
                            value_date=line.value_date,
                            # match_state is left at its default. Matching has not run — it is
                            # M2.2 — and pre-setting it would ship an answer with the question.
                        )
                    )
    finally:
        await engine.dispose()

    return {
        "settlement_batch": len(loaded.corpus.batches),
        "settlement_line": loaded.corpus.line_count,
        "ledger_entry": len(loaded.corpus.ledger_entries),
    }


async def _delete_owned_rows(session: AsyncSession, loaded: LoadedCorpus) -> None:
    """Delete exactly the rows this corpus owns, by identifier.

    Never ``TRUNCATE``, never ``CASCADE`` across the schema. Deterministic identifiers make
    precise deletion possible, and precision is what keeps this safe to run against a database
    that also holds something else.
    """
    line_ids = [line.id for batch in loaded.corpus.batches for line in batch.lines]
    batch_ids = [batch.id for batch in loaded.corpus.batches]
    entry_ids = [entry.id for entry in loaded.corpus.ledger_entries]

    if line_ids:
        await session.execute(delete(SettlementLine).where(SettlementLine.id.in_(line_ids)))
    if batch_ids:
        await session.execute(delete(SettlementBatch).where(SettlementBatch.id.in_(batch_ids)))
    if entry_ids:
        await session.execute(delete(LedgerEntry).where(LedgerEntry.id.in_(entry_ids)))


async def count_loaded(settings: Settings, loaded: LoadedCorpus) -> dict[str, int]:
    """Count the corpus's own rows in the database. Used by tests to verify a load."""
    assert_target_is_disposable(settings)
    engine = create_engine(settings)
    try:
        async with AsyncSession(engine) as session:
            batch_ids = [batch.id for batch in loaded.corpus.batches]
            line_ids = [line.id for batch in loaded.corpus.batches for line in batch.lines]
            entry_ids = [entry.id for entry in loaded.corpus.ledger_entries]
            return {
                "settlement_batch": len(
                    (
                        await session.execute(
                            select(SettlementBatch.id).where(SettlementBatch.id.in_(batch_ids))
                        )
                    ).all()
                ),
                "settlement_line": len(
                    (
                        await session.execute(
                            select(SettlementLine.id).where(SettlementLine.id.in_(line_ids))
                        )
                    ).all()
                ),
                "ledger_entry": len(
                    (
                        await session.execute(
                            select(LedgerEntry.id).where(LedgerEntry.id.in_(entry_ids))
                        )
                    ).all()
                ),
            }
    finally:
        await engine.dispose()
