"""Three pre-deployment exposure checks, over tracked files only.

    python deployment/checks/scan.py all
    python deployment/checks/scan.py secrets | config | frontend

**Why a committed script rather than a hosted scanner.** `CLAUDE.md` rule 7 requires the staged
diff to be inspected for secrets *before every commit*, and `PROJECT_SPEC.md` §17 requires the same
of the repository as a whole. A check that only exists inside a GitHub Action cannot serve that
rule: it runs after the mistake is already pushed, and it cannot be run by the person about to make
it. This file runs identically on a laptop and in CI, needs no account, no licence key and no
network, and its rules are readable and arguable in review.

**What this is not.** It is not a general-purpose secret scanner and must not be described as one.
It detects the credential shapes *this* repository could plausibly leak — the provider keys it
would use, the platform tokens its deployment needs, private keys, and connection strings carrying
a password — plus the configuration mistakes that would expose them. It does not do entropy
analysis, it does not read git history, and a clean run is evidence that these patterns are absent,
not that no secret exists. GitHub's own push protection and secret scanning are a separate,
platform-side control and are listed as an owner setup step in `docs/deployment.md`.

**Scope: tracked files.** ``git ls-files`` is the input, so the subject is what is committed rather
than whatever happens to be sitting in the working tree. Untracked scratch files are the
developer's business; a tracked one is everybody's.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import pathlib
import re
import subprocess
from collections.abc import Iterable, Iterator
from typing import Final

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[2]

#: Suffixes worth reading. Everything else is skipped as binary or generated: a scanner that reads
#: a 4 MB lockfile line by line for token shapes finds nothing and slows every build.
TEXT_SUFFIXES: Final = frozenset(
    {
        "",
        ".cfg",
        ".conf",
        ".css",
        ".env",
        ".example",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".jsx",
        ".md",
        ".mjs",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)

#: Paths never scanned for secret shapes, and why each one has to be.
#:
#: ``uv.lock`` carries a hash for every wheel — high-entropy by construction, and not credentials.
#:
#: This file holds the falsifiability battery below, whose whole job is to contain a credible
#: example of every shape the patterns look for. Scanning it would report those, permanently. The
#: exclusion is narrow and named rather than a wildcard, the file stays in scope for the
#: configuration check, and it is short enough to read in review — which is the only control that
#: stops it becoming a hiding place.
SECRET_SCAN_EXCLUDED: Final = ("uv.lock", "deployment/checks/scan.py")

#: Credential shapes. Each entry is a name and a pattern; the name is what a red run reports, so it
#: says what was found rather than which regex matched.
SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("a private key block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("an Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")),
    ("an OpenAI API key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("a GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("a Fly.io API token", re.compile(r"FlyV1\s+fm2_[A-Za-z0-9+/=]{10,}")),
    ("an AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("a Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("a Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("a Langfuse secret key", re.compile(r"\bsk-lf-[0-9a-f-]{16,}")),
    # A URL carrying userinfo. The password half is what matters, so it is captured and judged
    # separately: `postgresql://lecp@host/db` is a username, not a credential.
    (
        "a connection string with a password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[A-Za-z0-9._%+-]+:([^\s\"'/@]+)@[A-Za-z0-9._{$-]+"),
    ),
    # An assignment whose *name* says credential and whose value is long enough to be one.
    (
        "a credential-shaped assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|"
            r"client[_-]?secret|private[_-]?key)\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-+/=]{20,})"
        ),
    ),
)

#: Literal values that match a pattern above but are documented, development-only, scoped
#: credentials. A scanner that cannot express "this one is deliberate" gets switched off, which is
#: the real failure mode — but a *path* allowlist would be worse, because it turns off detection
#: for whole files. So this list holds values, not locations, and everything else that a test needs
#: to look like a secret is handled by :func:`looks_synthetic` on its own shape.
#:
#: `lecp_local_dev` is the Compose stack's password, on a localhost-bound database holding no real
#: data (`docker-compose.yml`); `lecp_ci_ephemeral` belongs to a service container that lives for
#: the length of one job. Neither is reachable from anywhere and neither may be reused by a
#: deployment — `config.py` and `.env.example` both say so.
PLACEHOLDER_VALUES: Final = (
    "lecp_local_dev",
    "lecp_ci_ephemeral",
)

#: Values nobody protects anything real with. A test needs a password-shaped string, and one of
#: these is what it reaches for.
WEAK_VALUES: Final = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "changeme",
        "hunter2",
        "postgres",
        "letmein",
        "correcthorsebatterystaple",
    }
)

#: Words that declare a value to be an illustration. `AKIAIOSFODNN7EXAMPLE` is AWS's own
#: documentation key and appears in every tutorial ever written about credential scanning.
ILLUSTRATION_MARKERS: Final = (
    "example",
    "sample",
    "placeholder",
    "redact",
    "scrub",
    "dummy",
    "fake",
    "notreal",
    "replace_me",
    "your_",
)

#: Below this many characters a value is not a credible credential, whatever it is called.
MINIMUM_CREDIBLE_LENGTH: Final = 8

#: A run of this many consecutive capitals marks a value as hand-written rather than generated.
#: Every provider in `SECRET_PATTERNS` issues mixed-case base62 or base64, so a long all-caps span
#: is a word somebody typed. **The known cost:** an AWS access key id is all-caps by format, so one
#: without digits would be judged synthetic here. Accepted deliberately — this project holds no AWS
#: credential, and provider keys in general are also covered by GitHub's platform-side secret
#: scanning, which `docs/deployment.md` lists as an owner setup step.
CAPITAL_RUN_LENGTH: Final = 12


def looks_synthetic(value: str) -> bool:
    """Whether a matched value is a stand-in rather than a credential.

    Judged on the value's own shape, so it needs no per-file exemption. Every rule below describes
    something a real generated credential is not:

    * it is not a template — a value containing ``{}``, ``$``, ``<`` or ``>`` is a substitution
      site, which is what an f-string or an ``.env.example`` line looks like;
    * it is not three characters long;
    * it is not ``password``;
    * it does not say ``EXAMPLE`` in the middle of itself;
    * it does not run ``0123456789`` or ``abcdefghij``, and it does not repeat one character six
      times. Both are what somebody types when they need a key-shaped string and nothing more.
    * it is not a long all-caps word — see ``CAPITAL_RUN_LENGTH`` for the one credential class
      that rule knowingly gives up on.

    A real Anthropic key, Fly token or Neon connection string satisfies none of these and is
    reported.
    """
    if any(character in value for character in "{}$<>"):
        return True
    if len(value) < MINIMUM_CREDIBLE_LENGTH:
        return True
    lowered = value.lower()
    if lowered.strip("'\"") in WEAK_VALUES:
        return True
    if any(marker in lowered for marker in ILLUSTRATION_MARKERS):
        return True
    if re.search(r"(.)\1{5,}", value):
        return True
    if re.search(rf"[A-Z]{{{CAPITAL_RUN_LENGTH},}}", value):
        return True
    return _has_sequential_run(value, length=8)


def _has_sequential_run(value: str, *, length: int) -> bool:
    """Whether ``value`` contains ``length`` consecutive code points in ascending order."""
    run = 1
    for previous, current in itertools.pairwise(value):
        run = run + 1 if ord(current) == ord(previous) + 1 else 1
        if run >= length:
            return True
    return False


def matched_value(pattern: re.Pattern[str], line: str) -> str | None:
    """The part of ``line`` a pattern objects to: its first capture group, or the whole match."""
    match = pattern.search(line)
    if match is None:
        return None
    return match.group(1) if match.groups() else match.group(0)


#: Filenames that must never be tracked, whatever they contain.
FORBIDDEN_TRACKED: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"(?:^|/)\.env$"),
    re.compile(r"(?:^|/)\.env\.(?!example$)[A-Za-z0-9_.-]+$"),
    re.compile(r"\.pem$"),
    re.compile(r"\.p12$"),
    re.compile(r"\.pfx$"),
    re.compile(r"\.jks$"),
    re.compile(r"(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)$"),
    re.compile(r"(?:^|/)\.npmrc$"),
    re.compile(r"(?:^|/)\.pypirc$"),
    re.compile(r"(?:^|/)\.netrc$"),
)

#: `.gitignore` must contain each of these, or the corresponding class of file can be committed by
#: accident. Checked as text because that is what git reads.
REQUIRED_GITIGNORE: Final = (".env",)

#: Frontend build tools inline any variable with one of these prefixes into the browser bundle.
#: A secret-sounding name behind one of them is a secret published to every visitor.
PUBLIC_ENV_PREFIXES: Final = ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "PUBLIC_", "NUXT_PUBLIC_")

#: Name fragments that make a public variable a credential exposure rather than configuration.
CREDENTIAL_NAME_FRAGMENTS: Final = ("TOKEN", "SECRET", "PASSWORD", "APIKEY", "API_KEY", "PRIVATE")

#: Server-side names that must never appear anywhere under the frontend directory: the principal
#: registry, the platform tokens, and the database. A frontend that knows any of them has been
#: handed the keys to the control plane.
SERVER_ONLY_NAMES: Final = (
    "LECP_PRINCIPALS",
    "LECP_POSTGRES_DSN",
    "LECP_REDIS_DSN",
    "FLY_API_TOKEN",
    "NEON_API_KEY",
    "DATABASE_URL",
)

#: GitHub Actions triggers that run with a writable token in the context of a fork's pull request.
#: `pull_request_target` and `issue_comment` are the two classic credential-exposure footguns.
DANGEROUS_TRIGGERS: Final = ("pull_request_target", "issue_comment")

#: Expressions that must never be interpolated into a `run:` block: the shell would execute
#: whatever they contain. Secrets go through `env:`; untrusted event text goes nowhere.
UNSAFE_INTERPOLATIONS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"\$\{\{\s*secrets\."),
    re.compile(r"\$\{\{\s*github\.event\.(?:issue|pull_request|comment|review)\."),
    re.compile(r"\$\{\{\s*github\.head_ref"),
)


@dataclasses.dataclass(frozen=True, slots=True)
class Finding:
    """One problem, located precisely enough to fix without searching."""

    path: str
    line: int
    message: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  {where}: {self.message}"


def tracked_files() -> list[str]:
    """Every file git is tracking, as repo-relative POSIX paths."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [entry for entry in result.stdout.split("\0") if entry]


def readable_lines(relative: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line number, text)`` for a tracked text file, skipping what cannot be read."""
    path = REPO_ROOT / relative
    if path.suffix.lower() not in TEXT_SUFFIXES or not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return
    yield from enumerate(content.splitlines(), start=1)


def secret_findings(relative: str, number: int, line: str) -> Finding | None:
    """The first credible credential shape on one line, or ``None``."""
    if any(value in line for value in PLACEHOLDER_VALUES):
        return None
    for description, pattern in SECRET_PATTERNS:
        value = matched_value(pattern, line)
        if value is not None and not looks_synthetic(value):
            return Finding(relative, number, f"looks like {description}")
    return None


# ======================================================================================
# Check 1 — tracked secrets
# ======================================================================================


def scan_secrets(files: Iterable[str]) -> list[Finding]:
    """Credential shapes in tracked text files."""
    findings: list[Finding] = []
    for relative in files:
        if relative in SECRET_SCAN_EXCLUDED:
            continue
        for number, line in readable_lines(relative):
            finding = secret_findings(relative, number, line)
            if finding is not None:
                findings.append(finding)
    return findings


# ======================================================================================
# Check 2 — unsafe configuration and credential exposure
# ======================================================================================


def scan_config(files: Iterable[str]) -> list[Finding]:
    """Tracked files that must not exist, a `.gitignore` that must, and workflow hygiene."""
    findings: list[Finding] = []
    tracked = list(files)

    for relative in tracked:
        for pattern in FORBIDDEN_TRACKED:
            if pattern.search(relative):
                findings.append(Finding(relative, 0, "this file must never be tracked"))
                break

    gitignore_path = REPO_ROOT / ".gitignore"
    gitignore = gitignore_path.read_text(encoding="utf-8") if gitignore_path.is_file() else ""
    for required in REQUIRED_GITIGNORE:
        if not any(line.strip() == required for line in gitignore.splitlines()):
            findings.append(Finding(".gitignore", 0, f"does not ignore {required!r}"))

    findings.extend(scan_workflows(tracked))
    return findings


def scan_workflows(files: Iterable[str]) -> list[Finding]:
    """Workflow-level credential hygiene.

    Three rules, each of which has cost somebody a token in public:

    * ``pull_request_target`` and ``issue_comment`` run with the repository's own credentials in
      the context of code the author does not control.
    * A secret interpolated into a ``run:`` block becomes part of a shell command line. It belongs
      in ``env:``, where the value is passed to the process rather than pasted into the script.
    * Untrusted event text — a branch name, an issue title — interpolated into ``run:`` is remote
      code execution with the workflow's permissions.
    """
    findings: list[Finding] = []
    for relative in files:
        if not relative.startswith(".github/workflows/"):
            continue

        in_run = False
        run_indent = 0
        for number, line in readable_lines(relative):
            stripped = line.strip()

            for trigger in DANGEROUS_TRIGGERS:
                if stripped.startswith(f"{trigger}:"):
                    findings.append(
                        Finding(
                            relative, number, f"{trigger} runs with a writable token on fork code"
                        )
                    )

            indent = len(line) - len(line.lstrip())
            if in_run and stripped and indent <= run_indent:
                in_run = False
            if stripped.startswith(("run:", "- run:")):
                in_run = True
                run_indent = indent
            if in_run:
                for pattern in UNSAFE_INTERPOLATIONS:
                    if pattern.search(line):
                        findings.append(
                            Finding(
                                relative,
                                number,
                                "an expression is interpolated into a shell command; pass it "
                                "through `env:` instead",
                            )
                        )
    return findings


# ======================================================================================
# Check 3 — frontend secret exposure
# ======================================================================================

FRONTEND_DIRECTORIES: Final = ("frontend", "web")


def scan_frontend(files: Iterable[str]) -> tuple[list[Finding], str]:
    """Secrets reachable from browser code, and public variables named like credentials.

    Returns the findings and a one-line note about what was in scope, because "no findings"
    means two very different things depending on whether the directory exists. The frontend is
    built by a separate increment; until it lands this check reports that it found nothing to
    scan, and it starts doing real work the moment the directory appears.
    """
    candidates = [
        relative
        for relative in files
        if relative.split("/", 1)[0] in FRONTEND_DIRECTORIES
        and "/node_modules/" not in f"/{relative}"
        and "/dist/" not in f"/{relative}"
    ]
    if not candidates:
        present = ", ".join(FRONTEND_DIRECTORIES)
        return [], f"no tracked files under {present}/ — nothing to scan yet"

    findings: list[Finding] = []
    for relative in candidates:
        for pattern in FORBIDDEN_TRACKED:
            if pattern.search(relative):
                findings.append(Finding(relative, 0, "this file must never be tracked"))

        for number, line in readable_lines(relative):
            for name in SERVER_ONLY_NAMES:
                if name in line:
                    findings.append(
                        Finding(
                            relative, number, f"{name} is server-side only and must not appear here"
                        )
                    )
            for match in re.finditer(r"\b([A-Z][A-Z0-9_]{2,})\b", line):
                variable = match.group(1)
                if not variable.startswith(PUBLIC_ENV_PREFIXES):
                    continue
                if any(fragment in variable for fragment in CREDENTIAL_NAME_FRAGMENTS):
                    findings.append(
                        Finding(
                            relative,
                            number,
                            f"{variable} is inlined into the browser bundle by its prefix; "
                            "a credential must be read server-side",
                        )
                    )
            finding = secret_findings(relative, number, line)
            if finding is not None:
                findings.append(finding)

    return findings, f"{len(candidates)} tracked file(s) scanned"


# ======================================================================================
# Falsifiability — a scanner that passes on everything is worse than none
# ======================================================================================

#: Lines that MUST be detected. Every value is synthesised here and satisfies none of the
#: :func:`looks_synthetic` rules, so it stands in for the real thing without being one: mixed case,
#: no repeats, no sequences, no illustration word.
MUST_DETECT: Final[tuple[tuple[str, str], ...]] = (
    ("an Anthropic key", 'ANTHROPIC_API_KEY = "sk-ant-api03-c7Kq2vTn9bXr4Zm1Ld8Wp3Hs6Yf5Gj0Qa"'),
    ("an OpenAI key", 'key = "sk-proj-Kq7vTn2bXr9Zm4Ld1Wp8Hs3Yf6Gj5Qa0Nc7Bv2Mx"'),
    ("a GitHub PAT", "token: ghp_Kq7vTn2bXr9Zm4Ld1Wp8Hs3Yf6Gj5Qa"),
    ("a Fly.io token", 'FLY_API_TOKEN = "FlyV1 fm2_Kq7vTn2bXr9Zm4Ld1Wp8Hs3Yf6Gj5"'),
    ("a Neon connection string", "dsn = postgresql://lecp:Kq7vTn2bXr9Zm@ep-cool-fog.neon.tech/db"),
    ("a private key", "-----BEGIN OPENSSH PRIVATE KEY-----"),
    ("a Langfuse secret", 'secret_key = "sk-lf-3f7a91cc-2b48-4d1e-9c60-ab7513ef2d84"'),
    ("a Slack token", "xoxb-3f7a91cc2b48-4d1e9c60ab75-Kq7vTn2bXr9Zm4Ld1Wp8"),
    ("a credential-shaped assignment", 'client_secret: "Kq7vTn2bXr9Zm4Ld1Wp8Hs3Yf6Gj5Qa"'),
)

#: Lines that must NOT be detected. Each is a shape this repository legitimately contains; a
#: scanner that reports them is a scanner somebody turns off.
MUST_IGNORE: Final[tuple[tuple[str, str], ...]] = (
    ("the Compose stack password", "postgresql://lecp:lecp_local_dev@postgres:5432/lecp"),
    (
        "the CI service container password",
        "postgresql://lecp:lecp_ci_ephemeral@localhost/lecp_test",
    ),
    ("an f-string template", 'f"postgresql://lecp:{SECRET_PASSWORD}@db:5432/lecp"'),
    ("a one-character test password", "postgresql://u:p@h:5432/d"),
    ("an environment-substituted DSN", "postgresql://lecp:${DB_PASSWORD}@host/db"),
    ("AWS's documentation key", 'aws_key = "AKIAIOSFODNN7EXAMPLE"'),
    ("a scrubbing test's stand-in", '"Authorization": "Bearer sk-ant-api03-REDACTMEREDACTME"'),
    ("a sequential stand-in", '"X-API-Key": "sk-proj-0123456789abcdefghij"'),
    ("a token hash, which is not a token", '"token_sha256": "' + "0" * 64 + '"'),
)


def selftest() -> tuple[list[Finding], str]:
    """Plant a credible credential of every shape and assert the detector reports each one."""
    failures: list[Finding] = []

    for description, line in MUST_DETECT:
        if secret_findings("<selftest>", 0, line) is None:
            failures.append(Finding("<selftest>", 0, f"MISSED {description}"))

    for description, line in MUST_IGNORE:
        finding = secret_findings("<selftest>", 0, line)
        if finding is not None:
            failures.append(
                Finding("<selftest>", 0, f"false positive on {description}: {finding.message}")
            )

    planted = len(MUST_DETECT) + len(MUST_IGNORE)
    return (
        failures,
        f"{planted} planted line(s): {len(MUST_DETECT)} must fire, {len(MUST_IGNORE)} must not",
    )


# ======================================================================================
# CLI
# ======================================================================================

GROUPS: Final = ("secrets", "config", "frontend", "selftest")


def run_group(group: str, files: list[str]) -> tuple[list[Finding], str]:
    if group == "secrets":
        return scan_secrets(files), f"{len(files)} tracked file(s) considered"
    if group == "config":
        return scan_config(files), "tracked filenames, .gitignore and workflow hygiene"
    if group == "selftest":
        return selftest()
    return scan_frontend(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python deployment/checks/scan.py",
        description="Pre-deployment exposure checks over tracked files.",
    )
    parser.add_argument("group", choices=(*GROUPS, "all"))
    arguments = parser.parse_args(argv)

    files = tracked_files()
    groups = GROUPS if arguments.group == "all" else (arguments.group,)

    total = 0
    for group in groups:
        findings, note = run_group(group, files)
        total += len(findings)
        status = "FAIL" if findings else "PASS"
        print(f"  {status}  {group:<9}  {note}")
        for finding in findings:
            print(finding.render())

    if total:
        print(f"\nscan: FAILED ({total} finding(s))")
        return 1
    print("\nscan: OK")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
