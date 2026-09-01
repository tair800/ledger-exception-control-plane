"""The integration database must stay reproducible from a clean checkout.

Every integration module targets ``lecp_test``, and for four milestones nothing in the repository
created it. CI never noticed, because the service container gets the database free from
``POSTGRES_DB``; locally it survived as state on one machine, until rebuilding a volume destroyed it
and produced 162 setup errors that read like a code regression.

The fix is one Makefile target. These tests are what stop it from quietly coming undone — a
prerequisite dropped during an edit would restore the original defect and nothing else would say so
until the next clean environment, which by construction is the moment nobody is expecting it.

Text-level assertions on a Makefile, deliberately. ``make`` is not a dependency of this project and
is absent on some developer machines, so parsing the file is the only check that runs everywhere,
including in CI where no Docker daemon is available to the unit job.
"""

from __future__ import annotations

import pathlib
import re
from typing import Final

import pytest

from ledger_exception_control_plane.fixtures.loader import DISPOSABLE_DATABASE

MAKEFILE: Final = pathlib.Path(__file__).resolve().parents[1] / "Makefile"

#: The target that creates the disposable database.
BOOTSTRAP: Final = "test-db-init"

#: Targets that run something requiring the database to exist. Each must pull the bootstrap in,
#: because "run make db-up first" is exactly the instruction a clean environment cannot follow —
#: the container starting is not the same thing as the database existing.
NEEDS_THE_DATABASE: Final = (
    "coverage-gate",
    "smoke",
    "schema-verify",
    "fixtures-load",
    "fixtures-verify",
    "ingest-verify",
    "match-verify",
    "classify-verify",
)


def _makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _prerequisites(target: str, source: str) -> list[str]:
    match = re.search(rf"^{re.escape(target)}:([^\n#]*)", source, re.M)
    assert match, f"the Makefile has no {target} target"
    return match.group(1).split()


def _recipe(target: str, source: str) -> str:
    match = re.search(rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n)*)", source, re.M)
    assert match, f"the Makefile has no {target} target"
    return match.group(1)


def test_the_bootstrap_target_exists() -> None:
    assert re.search(rf"^{BOOTSTRAP}:", _makefile(), re.M), (
        f"{BOOTSTRAP} is what makes a clean checkout able to run the integration suite"
    )


@pytest.mark.parametrize("target", NEEDS_THE_DATABASE)
def test_every_database_backed_target_bootstraps_first(target: str) -> None:
    """The prerequisite, not a sentence in the help text, is what makes this reproducible."""
    assert BOOTSTRAP in _prerequisites(target, _makefile()), (
        f"{target} needs the database but does not depend on {BOOTSTRAP}"
    )


@pytest.mark.parametrize("target", NEEDS_THE_DATABASE)
def test_every_database_backed_target_talks_to_the_database_it_created(target: str) -> None:
    """Creating one database and then connecting to another is worse than creating none.

    ``test-db-init`` honours ``LECP_TEST_DB``; the integration modules default to ``lecp_test`` and
    read ``LECP_POSTGRES_DSN`` when it is set. Until every recipe exported that DSN, overriding the
    name produced ``created lecp_demo``, exit 0, and then a suite failing on ``lecp_test`` — the
    same "database does not exist" class of error this whole target exists to remove, now with a
    success line in front of it. ``fixtures-load`` had the sharper version: it resolved the name
    through the variable but the *port* through a literal, so pointing ``COMPOSE`` at a throwaway
    project created a database there and then reset the corpus in the developer's real one.
    """
    recipe = _recipe(target, _makefile())
    assert "LECP_POSTGRES_DSN=$(LECP_TEST_DSN)" in recipe, (
        f"{target} runs against the database the modules happen to default to, "
        "not the one test-db-init created"
    )
    assert "localhost:15432" not in recipe, f"{target} hardcodes the port instead of using the DSN"


def test_the_single_dsn_is_built_from_the_name_and_port_variables() -> None:
    """One definition, so the name and the instance cannot drift apart again."""
    source = _makefile()
    dsn = re.search(r"^LECP_TEST_DSN = (.+)$", source, re.M)
    assert dsn, "there is no single DSN definition"
    assert "$(LECP_TEST_DB)" in dsn.group(1)
    assert "$(LECP_DB_PORT)" in dsn.group(1)


def test_the_bootstrap_target_captures_the_name_before_using_it() -> None:
    """The name reaches the shell once, single-quoted, and is read back as a variable after.

    Interpolating ``$(LECP_TEST_DB)`` into the recipe's double-quoted messages let a backtick in
    the value execute while the guard was composing its refusal — and the refusal then printed a
    permitted name, because the substitution had consumed the payload.
    """
    recipe = _recipe(BOOTSTRAP, _makefile())
    assert "db='$(LECP_TEST_DB)'" in recipe, "the name is not captured into a quoted variable"

    body = recipe.split("db='$(LECP_TEST_DB)'", 1)[1]
    assert "$(LECP_TEST_DB)" not in body, (
        "the name is interpolated again after capture, which re-opens the quoting hole"
    )


def test_the_bootstrap_starts_the_service_it_needs() -> None:
    """Creating a database in a container that is not running is not a bootstrap."""
    assert "db-up" in _prerequisites(BOOTSTRAP, _makefile())


def test_the_bootstrap_is_idempotent_rather_than_unconditional() -> None:
    """It must ask before it creates. An unconditional ``createdb`` fails the second run."""
    recipe = _recipe(BOOTSTRAP, _makefile())
    assert "pg_database" in recipe, "the target does not check whether the database already exists"
    assert "already exists" in recipe, "the target does not report the no-op case"


def test_the_bootstrap_refuses_exactly_what_the_loader_refuses() -> None:
    """The two guards must name the same databases.

    The fixture loader already decides what "disposable" means (ADR-036). If the Makefile grew a
    fourth name, a database the loader would refuse to load into could still be created by tooling,
    and the two would disagree about what is safe.
    """
    recipe = _recipe(BOOTSTRAP, _makefile())

    # *Every* pattern line, not the first. Reading one was the bug a reviewer found: a second
    # `lecp_prod|lecp) ;;` line added underneath widened the guard while this test stayed green,
    # which is precisely the widening the paragraph above says it prevents.
    clauses = re.findall(r"^\s*([a-z_|]+)\)\s*;;", recipe, re.M)
    assert clauses, "the target has no name guard"

    names = {name for clause in clauses for name in clause.split("|")}
    assert names == {"lecp_test", "lecp_demo", "lecp_fixtures"}, (
        f"the guard permits {sorted(names)}"
    )
    for name in names:
        assert DISPOSABLE_DATABASE.fullmatch(name), f"{name} is not disposable to the loader"

    # And the ones that matter are refused: the project's real database, and anything else.
    for refused in ("lecp", "postgres", "lecp_prod"):
        assert refused not in names
        assert not DISPOSABLE_DATABASE.fullmatch(refused)


#: ``docker compose down`` asking for the volume, in either spelling. ``-v`` and ``--volumes`` are
#: synonyms, and matching only the short one is how a reviewer slipped a volume deletion into
#: ``db-up`` with this suite still green — which every database-backed target then routes through.
DESTROYS_VOLUMES: Final = re.compile(r"\bdown\b[^\n]*\s(?:-v\b|--volumes\b)")


def test_destruction_is_never_hidden_inside_an_ordinary_target() -> None:
    """Deleting the data volume is one target's job, and it must announce itself.

    A prerequisite chain that quietly reset the database would turn every integration run into data
    loss, and the failure would look like a flaky test rather than a deleted volume.

    One assertion over every recipe line in the file, rather than a per-target loop: the loop that
    was here first could not fail, because "exactly one line in the file destroys volumes and it
    belongs to down-volumes" already implies no other target contains one.
    """
    source = _makefile()
    destructive = [
        line
        for line in source.splitlines()
        if line.startswith("\t") and DESTROYS_VOLUMES.search(line)
    ]
    assert len(destructive) == 1, f"{len(destructive)} recipe lines destroy volumes: {destructive}"
    assert destructive[0] in _recipe("down-volumes", source), (
        f"a target other than down-volumes deletes the data volume: {destructive[0]!r}"
    )

    help_text = re.search(r"^down-volumes:[^\n]*## (.+)$", source, re.M)
    assert help_text, "down-volumes has no help text"
    assert "DESTRUCTIVE" in help_text.group(1), (
        "the destructive target must announce itself in `make help`"
    )
