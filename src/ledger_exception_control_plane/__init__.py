"""Ledger exception control plane.

Turns unmatched settlement lines into approved, once-only ledger adjustments.

At this milestone the package exists only so the tooling baseline — packaging, linting,
type checking and the test runner — has something real to operate on. No business logic
lives here yet; see ``IMPLEMENTATION_PLAN.md`` for the increment that introduces each part.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
