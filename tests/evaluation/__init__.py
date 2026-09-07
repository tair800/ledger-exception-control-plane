"""The evaluation harness — golden set, scorer and comparison (M6).

**Why this lives under `tests/` and not in the package.** It reads the answer key. `PROJECT_SPEC.md`
§20 wants *"a committed golden set of labelled exceptions with expected treatment codes"*, and a
label is fixture-construction truth: only a synthetic corpus knows what each case was built to be.
The firewall in `tests/test_evidence_assembly.py` states the rule — *"nothing that matches,
classifies, assembles evidence, prompts a model or prices money may see a construction label"* — and
`tests/cassette_builder.py` set the precedent, with the Makefile recording it in one line: *"A test
artifact is made on the test side of that fence."*

Putting an `evaluation` package in `src/` would have meant adding a third exemption to that guard.
Exempting a module from a firewall in order to build the thing the firewall exists to keep out is
the wrong direction, so the fence stayed where it was.

**What this harness may and may not conclude.** The committed cassettes are **synthesised, not
captured** — their recorded responses say so in the response body itself, and `CLAUDE.md` has
carried the warning since 3.4 precisely because *"6.3 will publish measurements produced from
cassettes and the difference must never be lost"*. So:

- Every number this harness produces carries the **origin** of the cassettes it came from.
- An accuracy figure computed over synthesised responses measures **this harness**, not a model, and
  :mod:`tests.evaluation.scorer` refuses to label it otherwise.
- The deterministic arm needs no model at all. Its accuracy is a real measurement today, and it is
  the arm the CI gate is set against.

Publishing a synthesised run as "treatment-proposal accuracy" would be inventing a metric, which
`CLAUDE.md` §10 forbids outright. The machinery is built and proven; the model-facing numbers wait
on real captures.
"""
