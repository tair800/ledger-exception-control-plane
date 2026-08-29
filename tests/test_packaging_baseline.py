"""Tests for the packaging and tooling baseline established in M0.1.

These are deliberately not smoke tests that assert a constant. Each one checks a real
invariant of the project skeleton that can genuinely break:

* the version declared in code and the version recorded in distribution metadata can
  drift apart the moment someone bumps one and forgets the other;
* CI, or a developer's shell, can silently run a Python other than the one the project
  declares — which is exactly the failure this milestone exists to rule out.

Both would pass unnoticed without a test, and both invalidate later measurements.
"""

from __future__ import annotations

import sys
from importlib import metadata

import ledger_exception_control_plane as pkg

DISTRIBUTION_NAME = "ledger-exception-control-plane"


def test_package_version_matches_installed_distribution_metadata() -> None:
    """``__version__`` must agree with the version the distribution was built with.

    This proves three things at once: the package imports, it is installed as a real
    distribution rather than merely being on ``sys.path``, and the two places a version
    is recorded have not drifted.
    """
    declared = pkg.__version__
    installed = metadata.version(DISTRIBUTION_NAME)

    assert declared == installed, (
        f"version drift: ledger_exception_control_plane.__version__ is {declared!r} "
        f"but the installed distribution reports {installed!r}. "
        f"Update both, or the build is describing something other than this code."
    )


def test_running_interpreter_satisfies_declared_requires_python() -> None:
    """The interpreter running the suite must be the 3.12 series the project declares.

    ``pyproject.toml`` pins ``requires-python = "==3.12.*"``. If the suite runs on a
    different minor version, the type checking and behaviour verified here are not the
    ones that will run in production, and CI would be green for the wrong environment.
    """
    requires_python = metadata.metadata(DISTRIBUTION_NAME)["Requires-Python"]
    running = f"{sys.version_info.major}.{sys.version_info.minor}"

    assert requires_python == "==3.12.*", (
        f"declared Requires-Python changed to {requires_python!r}; "
        f"update this test deliberately if that was intended."
    )
    assert running == "3.12", (
        f"suite is running on Python {running}, but the project declares "
        f"{requires_python!r}. Check the interpreter selection in your shell and in CI."
    )


def test_package_exposes_no_business_api_yet() -> None:
    """M0.1 is tooling only; the package root must not yet export business functionality.

    This guards the milestone boundary. Reconciliation, treatment proposals, adapters and
    the rest arrive in later increments and will extend ``__all__`` deliberately — this
    test is expected to be updated by the increment that adds the first real export, and
    to fail loudly if something is added without that thought.
    """
    assert pkg.__all__ == ["__version__"], (
        f"package root exports {pkg.__all__!r}. If an increment legitimately added a "
        f"public API, update this test as part of that increment."
    )
