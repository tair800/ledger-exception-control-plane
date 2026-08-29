"""Command line for the fixture corpus: ``generate``, ``verify``, ``load``.

``verify`` is the one that earns its place. It regenerates the corpus in memory and compares
bytes with what is committed, so drift between the generator and the checked-in artifacts
fails in CI rather than being discovered when a later milestone's test starts behaving oddly.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from ledger_exception_control_plane.config import Settings
from ledger_exception_control_plane.fixtures.generator import (
    BULK_DEFAULT_INSTANCES,
    MANIFEST_PATH,
    generate,
)
from ledger_exception_control_plane.fixtures.loader import UnsafeTargetError, load, read_corpus
from ledger_exception_control_plane.fixtures.schema import Profile

#: The seed the committed corpus is generated with. An arbitrary fixed constant — it is not
#: read as a date and nothing derives meaning from its value; it exists so the committed
#: artifacts have one reproducible origin.
DEFAULT_SEED = 20260829

#: Where the committed canonical corpus lives, relative to the repository root.
DEFAULT_CORPUS_DIR = Path("fixtures") / "canonical"


def write_corpus(root: Path, files: dict[str, bytes]) -> None:
    """Write every artifact, removing any file the generator no longer produces.

    Stale files matter: a renamed artifact left behind would still be picked up by the
    manifest digest on the next read, and the corpus would fail its own integrity check for a
    reason that has nothing to do with its contents.

    **The removal is why this function is guarded.** It deletes every file under ``root`` that
    is not one of the artifacts about to be written, and ``rglob`` matches dotted entries — so
    pointed at a repository root it would unlink the source tree and ``.git`` with it.
    ``--out`` is a free-form path and ``.`` is a plausible slip. ADR-035 guards the *loader*,
    whose writes are additive; this is the destructive one and had no guard at all until an
    adversarial review pointed it out.

    The rule: write into a directory that is empty, does not exist, or is already a corpus.
    A non-empty directory with no manifest is refused, because the generator cannot tell it
    from somebody's work.
    """
    if root.exists() and any(root.iterdir()) and not (root / MANIFEST_PATH).is_file():
        raise UnsafeTargetError(
            f"refusing to write a corpus into {root}: it is not empty and contains no "
            f"{MANIFEST_PATH}, so it is not a corpus this command may overwrite"
        )

    root.mkdir(parents=True, exist_ok=True)
    expected = {root / path for path in files}
    for existing in sorted(root.rglob("*")):
        if existing.is_file() and existing not in expected:
            existing.unlink()

    for path, payload in sorted(files.items()):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Binary, so nothing translates the newlines this corpus deliberately fixes to "\n".
        target.write_bytes(payload)


def _diff(committed: dict[str, bytes], regenerated: dict[str, bytes]) -> list[str]:
    problems = [f"missing: {path}" for path in sorted(set(regenerated) - set(committed))]
    problems += [f"unexpected: {path}" for path in sorted(set(committed) - set(regenerated))]
    problems += [
        f"differs: {path}"
        for path in sorted(set(committed) & set(regenerated))
        if committed[path] != regenerated[path]
    ]
    return problems


def _generate(args: argparse.Namespace) -> int:
    result = generate(args.seed, Profile(args.profile), args.instances)
    write_corpus(args.out, result.files)
    print(
        f"{result.manifest.scenario_count} scenarios, "
        f"{result.manifest.settlement_line_count} settlement lines, "
        f"{result.manifest.ledger_entry_count} ledger entries -> {args.out}"
    )
    print(f"content sha256 {result.manifest.content_sha256}")
    return 0


def _verify(args: argparse.Namespace) -> int:
    from ledger_exception_control_plane.fixtures.loader import corpus_files

    regenerated = generate(args.seed, Profile(args.profile), args.instances).files
    problems = _diff(corpus_files(args.dir), regenerated)
    if problems:
        print(f"corpus at {args.dir} has drifted from the generator:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("regenerate with: make fixtures", file=sys.stderr)
        return 1
    print(f"corpus at {args.dir} matches the generator byte for byte")
    return 0


def _load(args: argparse.Namespace) -> int:
    loaded = read_corpus(args.dir)
    written = asyncio.run(load(loaded, Settings(), reset=args.reset))
    print(", ".join(f"{table}: {count}" for table, count in sorted(written.items())))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ledger_exception_control_plane.fixtures",
        description="Deterministic settlement fixture corpus.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--seed", type=int, default=DEFAULT_SEED)
        sub.add_argument(
            "--profile", choices=[profile.value for profile in Profile], default=Profile.CANONICAL
        )
        sub.add_argument("--instances", type=int, default=BULK_DEFAULT_INSTANCES)

    generate_cmd = subcommands.add_parser("generate", help="write a corpus to a directory")
    common(generate_cmd)
    generate_cmd.add_argument("--out", type=Path, default=DEFAULT_CORPUS_DIR)
    generate_cmd.set_defaults(handler=_generate)

    verify_cmd = subcommands.add_parser("verify", help="fail if a corpus has drifted")
    common(verify_cmd)
    verify_cmd.add_argument("--dir", type=Path, default=DEFAULT_CORPUS_DIR)
    verify_cmd.set_defaults(handler=_verify)

    load_cmd = subcommands.add_parser("load", help="load a corpus into a disposable database")
    load_cmd.add_argument("--dir", type=Path, default=DEFAULT_CORPUS_DIR)
    load_cmd.add_argument(
        "--reset",
        action="store_true",
        help="delete this corpus's own rows first, by identifier — never TRUNCATE",
    )
    load_cmd.set_defaults(handler=_load)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: object = args.handler
    assert callable(handler)
    result = handler(args)
    assert isinstance(result, int)
    return result


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
