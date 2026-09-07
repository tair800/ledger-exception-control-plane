"""The chaos suite — §19's scenarios, both branches, three capability configurations (4.5).

This package is the flagship gate. `PROJECT_SPEC.md` §19:

    A committed chaos suite, run against **both** branches:
    - `naive/` — a deliberately unsafe baseline … **It must double-post.**
    - `main` — the real implementation. **It must not.**
    …
    **A suite that passes on both branches proves nothing and is a defect.**

**Structure, and why it is split this way.** One module holds the scenario vocabulary and the three
capability configurations; one runs every scenario against `main`; one runs the same scenarios
against `naive/`; and one renders the results table. The mutation battery that proves this
instrumentation can itself fail lives at ``tests/test_kill_test_falsifiability.py``, outside this
package on purpose: it needs no database, so it belongs in the suite that runs on every build rather
than behind a migrated schema. A scenario is defined once and both branches are pointed at it,
because a suite with two hand-written copies of each scenario is a suite whose branches can quietly
diverge — and the whole claim rests on them facing the same world.
"""
