"""M11.1 — the claims the documentation is not allowed to make, and the links it must not break.

`IMPLEMENTATION_PLAN.md` §11.1 asks for two checks by name: a link check, and *"a test asserting the
phrase 'exactly-once' appears nowhere in `src/`, `web/`, `docs/` or `README.md`. The rule-defining
documents — `CLAUDE.md`, `PROJECT_SPEC.md`, `DECISIONS.md` and this plan — are excluded by an
explicit allowlist, since they must quote the banned phrase in order to ban it."*

**Why a test and not a review note.** The phrase is not a style preference. Across a process
boundary the property it names does not exist, and a reviewer who finds it in a finance-ops
repository is entitled to stop reading — `CLAUDE.md` says so in as many words. It is also the
easiest thing to write by accident, because it is the phrase everybody reaches for when they
mean the conditional one. So the ban is mechanical, and the allowlist is explicit rather than a
substring exemption that would quietly cover a new file.

No database, no network.
"""

from __future__ import annotations

import pathlib
import re
from typing import Final

import pytest

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parent.parent

#: The claim no shipped file may make.
#:
#: **Two patterns, and the split is the whole difficulty of this check.** `CLAUDE.md` and the plan
#: both ban the *hyphenated* phrase by name, because that is the term of art for the guarantee that
#: does not exist across a process boundary. The unhyphenated words are a different matter: *"the
#: ledger applied it exactly once"* is a true statement about a measured count, and the chaos suite
#: makes exactly that measurement. Banning it outright would forbid the repository from stating its
#: own strongest result.
#:
#: So the term of art is banned unconditionally, and the loose words are banned only where they
#: modify a *guarantee* — delivery, semantics, a promise. This test's own falsification cases are
#: what forced the distinction: a single pattern rejected "applied exactly once at the ledger",
#: which is the sentence the kill test exists to be able to write.
BANNED_TERM: Final = re.compile(r"exactly-once|exactlyonce", re.IGNORECASE)
BANNED_CLAIM: Final = re.compile(
    r"exactly\s+once\s+(delivery|semantics|guarantee|processing)"
    r"|(guarantee[sd]?|promis\w+|ensur\w+)\s+(\w+\s+){0,3}exactly\s+once",
    re.IGNORECASE,
)


def _banned(line: str) -> bool:
    """Whether this line makes the claim, in either of its two shapes."""
    return bool(BANNED_TERM.search(line) or BANNED_CLAIM.search(line))


#: The four documents permitted to quote it, because each one's job includes forbidding it.
#:
#: Listed by exact name. A pattern would have been shorter and would have grown to cover whatever
#: someone added next; the point of an allowlist is that extending it is a visible edit.
ALLOWED: Final[frozenset[str]] = frozenset(
    {"CLAUDE.md", "PROJECT_SPEC.md", "DECISIONS.md", "IMPLEMENTATION_PLAN.md"}
)

#: Where the ban applies. `web/` is named in the plan and does not exist; the console lives in
#: `frontend/`, which is checked here **and** by its own suite. The plan predates the directory's
#: name, and applying the rule to the directory that exists is the obvious reading.
SCANNED_DIRECTORIES: Final = ("src", "frontend/src", "docs", "naive")
SCANNED_FILES: Final = ("README.md",)

#: Extensions worth reading. Lockfiles and build output are excluded because neither is prose and
#: `frontend/package-lock.json` is 500 KB of it.
TEXT_SUFFIXES: Final = frozenset({".py", ".md", ".ts", ".tsx", ".css", ".yml", ".yaml", ".toml"})

#: Directories never scanned, whatever they contain.
SKIPPED: Final = frozenset({"node_modules", ".next", "__pycache__", ".venv", ".git"})


def _scanned_paths() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for name in SCANNED_FILES:
        path = REPO_ROOT / name
        if path.is_file():
            found.append(path)
    for directory in SCANNED_DIRECTORIES:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                continue
            if SKIPPED & set(path.relative_to(REPO_ROOT).parts):
                continue
            found.append(path)
    return found


def test_the_scan_is_reaching_real_files() -> None:
    """A ban over an empty file list is a ban over nothing.

    Asserted separately because every other test here passes trivially if the walk is broken, and a
    silently-empty guard is the failure mode this whole module exists to prevent elsewhere.
    """
    paths = _scanned_paths()
    assert len(paths) > 100, f"the scan found only {len(paths)} files"

    names = {path.name for path in paths}
    assert "README.md" in names
    assert "routes.py" in names, "the shipped package is not being scanned"
    assert any(path.suffix == ".tsx" for path in paths), "the console is not being scanned"


def test_the_stronger_reliability_claim_is_made_nowhere() -> None:
    """**§11.1's named check.** The phrase is absent from everything that ships.

    The failure message names the file and the line, because the fix is always to rewrite that
    sentence into the conditional claim and never to widen the allowlist.
    """
    offenders: list[str] = []
    for path in _scanned_paths():
        if path.name in ALLOWED:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if _banned(line):
                relative = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{relative}:{number}: {line.strip()[:100]}")

    assert offenders == [], (
        "the impossible guarantee is claimed in a shipped file. Rewrite the sentence as the "
        "conditional claim — an effectively-once *effect*, named mechanism, and only where the "
        "adapter's capability permits it. Do not add the file to the allowlist:\n  "
        + "\n  ".join(offenders)
    )


def test_the_allowlisted_documents_do_quote_it() -> None:
    """The other direction, and the reason it matters.

    Each allowlisted file is exempt *because* it states the ban. If one stopped quoting the phrase,
    its exemption would have become a hole nobody was using — and the next person to add a file to
    the list would find a precedent for exempting something that never needed it.
    """
    for name in sorted(ALLOWED):
        path = REPO_ROOT / name
        assert path.is_file(), f"{name} is allowlisted but does not exist"
        assert BANNED_TERM.search(path.read_text(encoding="utf-8")), (
            f"{name} no longer quotes the banned phrase, so its exemption protects nothing; "
            "remove it from the allowlist"
        )


def test_the_ban_catches_the_spellings_a_writer_would_actually_use() -> None:
    """The pattern, falsified against the shapes it must catch and the ones it must not.

    Kept as a test because a ban is only as good as its pattern, and a hyphen is not the only way
    anyone writes this.
    """
    for caught in (
        "exactly-once",
        "Exactly-Once",
        "exactlyonce",
        "we guarantee exactly-once delivery",
        # The term of art with the hyphen dropped, still asserting the guarantee.
        "the outbox provides exactly once delivery",
        "exactly once semantics for every posting",
        "we guarantee exactly once",
    ):
        assert _banned(caught), f"the ban misses {caught!r}"

    for permitted in (
        "effectively-once effect",
        "at-least-once",
        "at most once per operation identifier",
        "exactly one financial effect",
        # A measured count, not a guarantee. The kill test's whole result is this sentence.
        "applied exactly once at the ledger",
        "the ledger applied the operation exactly once",
    ):
        assert not _banned(permitted), (
            f"the ban would reject {permitted!r}, which is a different and true claim"
        )


# ======================================================================================
# Links
# ======================================================================================

#: Matches a markdown link with a relative target. Absolute URLs are not checked: this is a link
#: *check*, not a network test, and a suite that reached out to github.com would fail offline.
MARKDOWN_LINK: Final = re.compile(r"\[[^\]]*\]\((?!https?://|mailto:|#)([^)]+)\)")


@pytest.mark.parametrize(
    "document",
    sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in [*(REPO_ROOT / "docs").glob("*.md"), REPO_ROOT / "README.md"]
        if path.is_file()
    ),
)
def test_every_relative_link_resolves(document: str) -> None:
    """§11.1's link check. A README that points at a file nobody moved is worth more than one that
    points at six that somebody did."""
    path = REPO_ROOT / document
    broken: list[str] = []
    for match in MARKDOWN_LINK.finditer(path.read_text(encoding="utf-8")):
        target = match.group(1).split("#", 1)[0].strip()
        if not target:
            continue
        if not (path.parent / target).exists():
            broken.append(target)

    assert broken == [], f"{document} links to files that do not exist: {sorted(set(broken))}"


def test_the_readme_leads_with_the_problem_rather_than_the_milestone_history() -> None:
    """**A recruiter-first ordering, asserted so it cannot drift back.**

    This README opened with a hundred and fifty lines of milestone status for most of the build.
    That is the right document for a maintainer resuming work and the wrong one for the audience
    that decides whether to keep reading, so the status moved below the argument.

    Checked as an ordering rather than as a word count: what matters is that the problem, what the
    system does and the evidence all appear before the history.
    """
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    order = [
        "## Why this exists",
        "## What it does",
        "## Kill-test evidence",
        "## Demo",
        "## Status",
    ]
    positions = []
    for heading in order:
        assert heading in text, f"the README has no {heading!r} section"
        positions.append(text.index(heading))

    assert positions == sorted(positions), (
        "the README's sections are out of order; the problem and the evidence must precede the "
        f"milestone history. Found at: {dict(zip(order, positions, strict=True))}"
    )
