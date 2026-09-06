"""Command line for the dead-letter queue: ``list`` and ``replay`` (increment 4.3).

`IMPLEMENTATION_PLAN.md` §4.3 asks for a ``replay`` CLI and gives its exit criterion as *"DLQ replay
demonstrated end to end"*. Acceptance criterion 8 states what "works" means, and states it as a
measurement taken at the ledger rather than at us:

    The DLQ replay CLI produces **exactly one applied posting** for the operation — verified by the
    simulated ledger's applied-count, not by our own records — and replay of an already-`CONFIRMED`
    operation applies nothing further.

Invoked the way every other command in this repository is::

    python -m ledger_exception_control_plane.operations list
    python -m ledger_exception_control_plane.operations replay --id <uuid>
    python -m ledger_exception_control_plane.operations replay --all --dry-run

**The adapter is named, never guessed.** ``--adapter`` takes a name from a closed registry and
defaults to the reference simulated ledger, which is the only adapter this repository has. A real
one would have to be added to the registry deliberately, which is the point: a replay re-sends an
irreversible financial write, and "whatever adapter happened to be importable" is not an acceptable
answer to *where did it go*.

**``--dry-run`` reports without sending**, and prints the same lines a real run would. It is not the
default, because the exit criterion is a demonstration that replay actually posts; a command whose
default does nothing would satisfy the letter of it and none of the intent.

**No credential is read, printed or logged here.** The database DSN comes from ``Settings`` and is
held as a secret; this module never renders it, and the only identifiers it prints are the dead
letter's id, a truncated operation identifier, and — for a confirmed posting — the reference the
ledger returned.

**No provider text is rendered either**, and that took a correction. The first version printed
``ReplayReport.detail`` unconditionally, which for a rejection is the ledger's own ``reason``
string: provider-controlled, unbounded, and displayed straight to a terminal. Only details this
module produced are printed now; a provider's words are recorded in the database, where they
belong, and are not re-emitted here.

**One bad entry does not end a batch.** A forged id, an entry somebody already replayed, or a
database failure on one row is reported as a line and the run continues — the first version let
``LookupError`` and ``ValueError`` escape as a traceback, which abandoned every remaining entry and
exited with a code that said nothing about how far it had got.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
import uuid
from collections.abc import Callable, Sequence
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn
from ledger_exception_control_plane.ledger.port import LedgerAdapter
from ledger_exception_control_plane.ledger.simulated import SimulatedLedger
from ledger_exception_control_plane.operations.retry import (
    ReplayOutcome,
    ReplayReport,
    pending_dead_letters,
    replay_dead_letter,
)

#: Adapters a replay may be pointed at, by name.
#:
#: A closed registry rather than an import path on the command line. An operator replaying a
#: financial write should not be able to name an arbitrary module, and the set of adapters whose
#: capabilities have been through the conformance suite is exactly this one.
ADAPTERS: Final[dict[str, Callable[[], LedgerAdapter]]] = {
    "simulated": SimulatedLedger,
}

#: How many entries one invocation works. Bounded because a replay sends irreversible financial
#: writes, and an unbounded ``--all`` against a large backlog is a decision nobody took explicitly.
_PAGE: Final = 100


#: Outcomes whose ``detail`` this module wrote itself and may therefore print.
#:
#: ``REJECTED`` is deliberately absent: its detail is the ledger's own reason string, which is
#: provider-controlled and unbounded. It is persisted, and an operator reads it from the database
#: rather than from a terminal line this module cannot bound.
_DETAIL_IS_OURS: Final = frozenset(
    {
        ReplayOutcome.ALREADY_CONFIRMED,
        ReplayOutcome.REFUSED,
        ReplayOutcome.NOT_SENT,
        ReplayOutcome.HELD,
    }
)


def _render(report: ReplayReport) -> str:
    """One line per entry, saying what actually happened rather than whether it succeeded."""
    reference = f" ref={report.posting_ref}" if report.posting_ref else ""
    detail = f" — {report.detail}" if report.detail and report.outcome in _DETAIL_IS_OURS else ""
    return (
        f"{report.dlq_id} {report.outcome.value:<18} "
        f"operation={report.operation_id[:12]}…{reference}{detail}"
    )


async def _replay(
    dsn: str,
    *,
    entries: Sequence[uuid.UUID] | None,
    adapter_name: str,
    dry_run: bool,
    now: dt.datetime,
) -> int:
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session, session.begin():
            if entries:
                targets = list(entries)
                truncated = False
            else:
                # One more than the page, so a full page can be distinguished from a queue that
                # happens to be exactly that long. The first version asked for a hundred and
                # reported whatever came back as "the queue", so an operator draining a backlog was
                # told it was empty while ninety more entries waited.
                page = list(await pending_dead_letters(session, limit=_PAGE + 1))
                truncated = len(page) > _PAGE
                targets = page[:_PAGE]

        if not targets:
            print("no pending dead letters")
            return 0

        if truncated:
            print(
                f"replaying the oldest {_PAGE} pending entries; more remain — "
                "run again when this pass finishes",
                file=sys.stderr,
            )

        if dry_run:
            for target in targets:
                print(f"{target} would be replayed via {adapter_name}")
            return 0

        adapter = ADAPTERS[adapter_name]()
        failures = 0
        for target in targets:
            try:
                report = await replay_dead_letter(engine, dlq_id=target, adapter=adapter, now=now)
            except (LookupError, ValueError) as refusal:
                # A forged id, or an entry somebody has already worked. Reported and skipped: one
                # bad row in a batch is not a reason to abandon the rest of the queue.
                print(f"{target} {'skipped':<18} — {refusal}")
                failures += 1
                continue
            print(_render(report))
            if not report.resolved:
                failures += 1

        # A non-zero exit when an entry could not be resolved, because a replay that silently left
        # the queue where it was is the failure mode an operator most needs to notice.
        return 1 if failures else 0
    finally:
        await engine.dispose()


async def _list(dsn: str) -> int:
    engine = create_async_engine(dsn)
    try:
        async with AsyncSession(engine) as session, session.begin():
            page = list(await pending_dead_letters(session, limit=_PAGE + 1))

        truncated = len(page) > _PAGE
        for entry in page[:_PAGE]:
            print(entry)

        # The count says what it is: a page, or the whole queue. Reporting a capped page as a total
        # is how a backlog gets mistaken for an empty queue.
        shown = min(len(page), _PAGE)
        print(f"{shown} pending{' (more remain)' if truncated else ''}", file=sys.stderr)
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ledger_exception_control_plane.operations",
        description="Inspect and replay the dead-letter queue.",
    )
    parser.add_argument("command", choices=("list", "replay"))
    parser.add_argument(
        "--id",
        dest="ids",
        action="append",
        type=uuid.UUID,
        help="a dead-letter id to replay; repeatable. Omit with --all to take the whole queue.",
    )
    parser.add_argument("--all", action="store_true", help="replay every pending entry")
    parser.add_argument("--adapter", choices=sorted(ADAPTERS), default="simulated")
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would be replayed and send nothing"
    )
    arguments = parser.parse_args(argv)

    settings = Settings()
    dsn = async_dsn(settings)

    if arguments.command == "list":
        return asyncio.run(_list(dsn))

    if not arguments.ids and not arguments.all:
        parser.error("replay needs --id <uuid> (repeatable) or --all")

    return asyncio.run(
        _replay(
            dsn,
            entries=arguments.ids,
            adapter_name=arguments.adapter,
            dry_run=arguments.dry_run,
            now=dt.datetime.now(tz=dt.UTC),
        )
    )


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
