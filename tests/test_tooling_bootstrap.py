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
    "operations-verify",
    "dispatch-verify",
    "retry-verify",
    "approval-verify",
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


# ======================================================================================
# The Makefile has to parse — and once did not
# ======================================================================================


def _literal_escapes(source: str) -> list[str]:
    """Non-recipe lines carrying a two-character ``\\n`` or ``\\t`` where a real one belongs.

    Scoped to non-recipe lines, and that scoping was a correction: the first version banned the
    sequence outright and immediately fired on the ``help`` target, whose awk format string
    contains a perfectly legitimate ``\\n``. A recipe is shell text and may say whatever it likes;
    a target line, a prerequisite list or a ``.PHONY`` continuation may not.
    """
    return [
        f"{number}: {line.strip()[:70]!r}"
        for number, line in enumerate(source.split("\n"), start=1)
        if not line.startswith("\t") and ("\\n" in line or "\\t" in line)
    ]


def test_no_line_continuation_is_a_literal_escape_sequence() -> None:
    """**A regression test for a defect this repository actually shipped.**

    ``make`` is not a dependency of this project and CI drives every suite directly rather than
    through a target, so nothing executed the Makefile between it being edited and this test being
    written. A patch script emitted the two characters ``\\`` and ``n`` where a backslash and a
    newline were intended, and the ``.PHONY`` continuation silently became one long line carrying a
    target named ``\\n``. Harmless by luck rather than by design — the same slip one line lower,
    inside a recipe, produces a command nobody can run.

    Cheap to check, and the only thing standing between a text-edited Makefile and a broken one.
    """
    assert _literal_escapes(_makefile()) == []


def test_kill_a_mangled_continuation_is_detected() -> None:
    """The guard above, killed — with the exact text that was in this repository.

    The second case is the control that forced the scoping: a recipe containing ``\\n`` inside a
    shell string is correct and must not be reported, which is what the ``help`` target does.
    """
    mangled = ".PHONY: alpha beta \\n        gamma\n"
    assert _literal_escapes(mangled) != []

    legitimate = "help:\n\t@awk '{printf \"%s\\n\", $$1}'\n"
    assert _literal_escapes(legitimate) == []


def test_every_recipe_line_begins_with_a_tab() -> None:
    """A recipe indented with spaces is not a recipe, and ``make`` says so at parse time."""
    offenders = [
        f"{number}: {line[:60]!r}"
        for number, line in enumerate(_makefile().split("\n"), start=1)
        if re.match(r"^ +(uv run|LECP_POSTGRES_DSN=|\$\(COMPOSE\)|@|db=)", line)
    ]
    assert offenders == [], f"recipe lines indented with spaces: {offenders}"


def _declared_phony(source: str) -> set[str]:
    """Every name on the ``.PHONY`` line, following its continuations."""
    match = re.search(r"^\.PHONY:((?:[^\n]*\\\n)*[^\n]*)", source, re.M)
    assert match, "the Makefile declares no .PHONY targets"
    return set(match.group(1).replace("\\", " ").split())


def _defined_targets(source: str) -> set[str]:
    """Every target with a rule, excluding variable assignments and pattern rules."""
    return {match.group(1) for match in re.finditer(r"^([a-z][a-z0-9-]*):(?!=)", source, re.M)}


def test_every_declared_phony_target_actually_exists() -> None:
    """A ``.PHONY`` entry naming nothing is dead text — and is what a mangled continuation leaves
    behind, since the junk fragment lands in the list and no rule ever matches it."""
    source = _makefile()
    assert _declared_phony(source) - _defined_targets(source) == set()


def test_every_target_is_declared_phony() -> None:
    """None of these targets produces a file of its own name, so all of them are phony.

    Stated as a rule rather than a convention: a target left out would still run today and would
    stop running the moment a file appeared with its name — which for ``build``, ``test`` or
    ``fixtures`` is not a remote possibility.
    """
    source = _makefile()
    assert _defined_targets(source) - _declared_phony(source) == set()
