"""Whether a configured database is one this system may destroy.

**Moved here out of `fixtures/loader.py`, and the move is the interesting part.** The check began
as a fixture-loading concern: refuse to load a corpus into anything but a throwaway database. It
then turned out to be the guard that makes *any* destructive operation safe to expose — the demo
reset endpoint needs exactly the same question answered before it deletes a row.

Importing it from `fixtures` was not an option. A committed firewall forbids every production
module from importing that package at all, at package granularity, because the corpus knows the
answer to every case it contains and an assembler able to read a construction label would be
holding an answer key. The right response to a guard objecting is almost never an exemption: this
function is a statement about a DSN, it was in the fixtures package for historical reasons, and it
belongs somewhere both the loader and the application may reach.

`fixtures/loader.py` re-exports these names, so its own callers are unchanged and there is exactly
one implementation of the rule.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.db.engine import async_dsn

__all__ = [
    "DISPOSABLE_DATABASE",
    "UnsafeTargetError",
    "assert_target_is_disposable",
    "database_name",
    "printable_database_name",
]

#: Databases a destructive operation may target. Deliberately a closed pattern rather than a
#: warning: the primary application database and anything unrecognised are both refused.
DISPOSABLE_DATABASE: Final = re.compile(r"^lecp_(test|demo|fixtures)$")

#: What a database name may look like before it is safe to put in an error message.
_PRINTABLE_NAME: Final = re.compile(r"^[A-Za-z0-9_.\-]{0,63}$")


class UnsafeTargetError(RuntimeError):
    """Raised when a target is not one this system may write to destructively.

    Covers both destructive targets: the database a corpus would be loaded into, and the directory
    a corpus would be written to. One idea, one exception.
    """


def database_name(settings: Settings) -> str:
    """The database a DSN points at.

    ``urlsplit`` ends the netloc at the first ``/``, so a password containing an unencoded slash
    pushes the rest of the credential — and the user, host and port — into what looks like a path.
    The value returned here is therefore *not* guaranteed to be only a database name, and callers
    must not print it blindly. See :func:`printable_database_name`.
    """
    return urlsplit(async_dsn(settings)).path.lstrip("/")


def printable_database_name(name: str) -> str:
    """A database name that is safe to put in an error message, or a placeholder.

    §17 treats an error message as a log line waiting to happen. A malformed DSN is exactly the
    case where the parsed "name" may carry credential material, and it is also exactly the case
    someone is most likely to be debugging with the message in front of them.
    """
    return repr(name) if _PRINTABLE_NAME.fullmatch(name) else "<not a valid database name>"


def assert_target_is_disposable(settings: Settings) -> str:
    """Refuse to act destructively on anything that is not obviously a throwaway database.

    Named databases only. Checking the port instead would be weaker — a disposable database can
    live on any port, and the primary one can live on the project's — and checking a flag would put
    the decision in whichever caller forgot to pass it.
    """
    name = database_name(settings)
    if not DISPOSABLE_DATABASE.fullmatch(name):
        raise UnsafeTargetError(
            f"refusing a destructive operation on {printable_database_name(name)}: "
            f"the target must match {DISPOSABLE_DATABASE.pattern}"
        )
    return name
