"""Command line for the M2 snapshot: ``render`` and ``verify``.

``verify`` earns its place the same way the fixture corpus's does. It re-renders the page in memory
and compares bytes with what is committed, so drift between the pipeline and the checked-in artifact
fails in CI rather than being noticed when someone opens a stale demo and believes it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ledger_exception_control_plane.demo.render import render
from ledger_exception_control_plane.demo.snapshot import (
    DEFAULT_INSTANCES,
    DEFAULT_SEED,
    build,
)
from ledger_exception_control_plane.fixtures.schema import Profile

#: Where the committed snapshot lives, relative to the repository root.
#:
#: ``artifacts/`` rather than anywhere under ``src/`` or ``docs/``: it is generated output,
#: it is not importable, and nothing in the product reads it.
DEFAULT_OUTPUT = Path("artifacts") / "m2-demo.html"


def _page(seed: int, instances: int) -> str:
    return render(build(seed=seed, profile=Profile.BULK, instances=instances))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m ledger_exception_control_plane.demo",
        description="Render the M2 pipeline snapshot from real pipeline output.",
    )
    parser.add_argument("command", choices=("render", "verify"))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--instances", type=int, default=DEFAULT_INSTANCES)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)

    page = _page(args.seed, args.instances)

    if args.command == "verify":
        if not args.out.exists():
            print(f"{args.out} does not exist — run `make m2-demo`", file=sys.stderr)
            return 1
        committed = args.out.read_text(encoding="utf-8")
        if committed != page:
            print(
                f"{args.out} has drifted from the pipeline it claims to show.\n"
                f"Regenerate it with `make m2-demo` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print(f"{args.out} matches the pipeline byte for byte")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Newline pinned rather than left to the platform: the committed artifact is compared byte for
    # byte, and a CRLF checkout would fail that comparison for no reason anyone could act on.
    args.out.write_text(page, encoding="utf-8", newline="\n")
    print(f"{args.out} — {len(page.encode('utf-8')):,} bytes")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
