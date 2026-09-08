# Evaluation (`PROJECT_SPEC.md` §20)

What is measured, what is not, and why the difference is enforced by code rather than by a caveat.

Read this before quoting any number out of `tests/golden/`.

---

## 1. The one-paragraph version

The deterministic layers of this system are measured. The model layer is **not**, because no model
has ever been called from this repository: there is no provider SDK in the dependency graph, nothing
under `llm/` imports an HTTP client, and no transport that speaks HTTP exists. The committed
cassettes are **synthesised** — written by `tests/cassette_builder.py`, never received from a
provider — so every figure computed from them is a fact about this harness. The harness is built so
that such a figure cannot be reported as anything else: `score()` takes a required response origin
with no default, `Score.headline()` refuses the word "accuracy" for a synthesised run, and
`arms.Figure` cannot hold a value without an origin or an absence without a stated reason.

---

## 2. The artefacts

| Artefact | What it is | Regenerate | Drift-checked |
|---|---|---|---|
| `tests/golden/treatment-golden.jsonl` | 250 labelled exceptions, `BULK` profile, seed 20260829 | `make golden` | `make golden-check`, and in the unit suite |
| `tests/golden/replay-baseline.json` | Every scorer figure for the offline cassette replay, both providers | `make eval-gate-update` | `make eval-gate`, and in the unit suite |
| `tests/golden/human-label-packet/` | The frozen hold-out slice, for independent human labelling | `make label-packet` | in the unit suite |
| the three-arm table | §20's comparison, rendered as markdown | `make eval-compare` | **no** — see §7 |

All four are generated. None is hand-written, and none may be edited in place.

---

## 3. The golden set, and the identity migration behind it

`tests/evaluation/golden.py` runs the shipped deterministic stages over a seeded corpus — `generate`,
`match`, `classify`, and the counterpart-period derivation — and labels every residual the matcher
left. Only the label is the harness's own judgement, and it lives in `tests/evaluation/labels.py`.

**A golden record is keyed by the corpus row's own `id`.** That was a fix, not an original design.
The first version derived its own `uuid5` from a content hash and a line number, which produced a
set whose keys existed nowhere else in the repository: the committed cassettes and the real
pipeline's `ExceptionSubject` are both keyed on the corpus row id, so the two artefacts M6 has to
join shared **no identifier at all** — 13 exceptions on each side of the canonical corpus, zero in
common. Every count still looked right, and nothing compared the identifiers.

The generator now takes its lines from `generated.corpus.batches[].lines[]` directly. What that
costs is one property: these records no longer pass through ingestion's parse boundary. That
boundary keeps its own suite, and
`test_the_golden_set_agrees_with_what_ingestion_parses_from_the_same_bytes` asserts that every
corpus row and what `interpret` produces from the very same bytes agree field for field — so the
equivalence the generator relies on is checked rather than assumed.
`test_the_golden_set_and_the_cassette_covered_subjects_join_completely` fails unless the overlap is
total, and says what to do about it. The record schema version is `2`; the field list did not
change, the meaning of the key did.

**The set is 85.6% one label**, and that is structural: only two of the four reachable
classifications are priceable at all, so for the other two the correct action is `escalate`. A model
answering `escalate` to everything scores 85.6% while deciding nothing, which is why the scorer
reports the constant-answer baseline, the accuracy on the 36 priceable records, and the abstention
rate split by whether escalating was correct. See ADR-060.

---

## 4. The CI gate (6.2) — a reproduction gate, not a quality gate

`make eval-gate` replays the committed cassette through the **real** proposal path —
`assemble_evidence` → `build_prompt` → `ReplayTransport` → the adapter's own `propose()` — for both
providers, scores the result against the canonical golden set, and compares every figure with
`tests/golden/replay-baseline.json`. Any difference exits non-zero and names the metric that moved.

**Read this before quoting the numbers in that file.** `stand_in_answer` assigns
`TreatmentCode[position % 4]` — round-robin over the exceptions in sorted-id order — so agreement
with the golden labels is arithmetic over two orderings and contains no judgement. The gate protects:

- the evidence a subject assembles, and its order;
- the prompt built from it, and therefore the request fingerprint that decides a cassette match;
- each adapter's parsing of its own vendor's response envelope;
- the golden set's labels and identifiers, via a digest of the rendered canonical set;
- the scorer's arithmetic, via every figure it reports.

It is not an accuracy threshold on model quality. **OPEN-6 — the accuracy and abstention thresholds
that should fail a build — stays open**, because it cannot be answered before a real capture exists;
a threshold set against synthesised responses would be a number invented to look like a gate.

The comparison is exact rather than banded: everything upstream is deterministic, so there is nothing
for a tolerance to absorb except a behaviour change. `--update` is the deliberate door, and what it
writes is what review then sees.

**The gate is proven to fail.** `tests/test_evaluation_gate.py` plants ten regressions in a copy of
the baseline — a removed correct answer, an accuracy nudged in the sixth decimal, a moved confusion
cell, a dropped confusion row, an inverted abstention split, a changed golden or cassette digest, a
rewritten model version, a synthesised run relabelled as captured, and a dropped provider — and
asserts a non-zero exit for each. The proof needs no CI run and cannot leave the committed artefact
wrong.

---

## 5. The human-labelled hold-out slice (OPEN-15)

§20 requires a human-labelled slice. **Nobody has labelled one**, every committed record says
`label_source: derived`, and a test pins that gap so it fails on the day a person confirms the
slice. What exists is the mechanism.

```
make label-packet                                            # writes the question
uv run python -m tests.evaluation import-labels <file.csv>    # validates a filled-in answer
```

`make label-packet` writes `tests/golden/human-label-packet/`: a CSV with a blank `HUMAN_LABEL`
column, a JSONL companion, and generated instructions. **No command in this repository writes a
label.**

What a labeller is shown is an allowlist — `PERMITTED_FIELDS` — not a denylist, so a field added to
`GoldenRecord` is absent from the packet until somebody decides it is safe to show. Absent: the
expected treatment, the label rule and its reasoning, any model proposal, and the corpus's
construction metadata.

**The account policy is withheld too, and that is the least obvious decision here.** For two of the
four reachable classes the derived label follows mechanically from the policy configuring nothing, so
a labeller shown that table would reproduce the derived label rather than test it — and this slice
exists to catch a wrong label table. The packet gives the exception's facts and a definition of each
treatment as an accounting action, and asks for an independent judgement. **Disagreement with the
derived label is the useful outcome, not an error in the row.**

`originating_period` *is* shown, and it is worth being explicit about why. Its presence distinguishes
the two cross-period-refund labels, so a labeller could in principle learn the pattern — but it is a
fact the system derives from production fields and uses to price the case, not an answer key.
Withholding it would leave a labeller unable to answer at all.

The slice is frozen: `HOLD_OUT_VERSION` plus a SHA-256 over exactly what a labeller was shown. An
import must declare both and match. The validator refuses rather than repairs — an incomplete id
set, a duplicate, an unknown or mis-cased label, a blank label, a label for a record not in the
slice, a stale version or digest, a mixed file, or a row with no `labelled_by`/`labelled_on` — and it
reports every reason rather than the first.

**Synthetic labels.** The format carries a `synthetic` marker so the validator's own mechanics can
be exercised without a person. A synthetic import is refused a label source outright: asking for one
raises, `is_evaluation_evidence` is `False`, and the CLI exits non-zero. It must never be reported as
human, counted in any figure, or written into the golden set.

Applying a confirmed import to the golden set is a **separate, reviewed change**: the label source
becomes `human`, the record carries who confirmed it and when, and the test asserting nobody has
confirmed one is updated in the same commit that makes it false.

**That change has now happened.** The owner labelled all 25 on 2026-09-09 and every label agreed
with the derived one. The confirmations are committed at `human-label-packet/confirmed.jsonl`, the
generator reads them, and 25 records carry `label_source: human` with an attribution. The test that
pinned the gap now pins its closure — that *exactly* the held-out records are human and the other
225 are untouched.

Three things about how that was applied are deliberate:

- **An agreeing confirmation is applied; a disagreeing one is refused.** `confirmations.py` raises
  rather than adopting a label that differs from the derived one. Agreement changes only
  provenance; disagreement would change the answer key, and one spreadsheet may not do that
  silently. The refusal branch never fired here, so it is tested directly.
- **No `expected_treatment` moved**, and the evaluation gate is what proves it: re-running it
  reported exactly two differences, the schema version and the set's hash, and no scorer figure.
- **The result is four independent judgements, not 25.** The derived label is a pure function of
  the classification across all 250 records, so the slice is four distinct questions repeated 3, 3,
  10 and 9 times. It establishes the label table is right about its four classes. Quoting it as
  "25/25" without that qualifier overstates its resolution roughly six-fold.

---

## 6. The three-arm comparison (6.3)

`make eval-compare` renders §20's table. What it can and cannot measure:

| Arm | Accuracy | USD / 1,000 lines | p95 |
|---|---|---|---|
| deterministic matcher | measured (`no_model`) | 0, structurally | measured |
| LLM-as-matcher | `NOT MEASURED` | `NOT MEASURED` | `NOT MEASURED` |
| shipped hybrid | deterministic half, measured | `NOT MEASURED` | `NOT MEASURED` |

**The deterministic arm's accuracy is pair precision** — correct pairs over pairs produced — and that
was a choice over a per-line rate. The corpus labels each *scenario* with a match intent, not each
line with "should this have matched", and several residual-intent scenarios legitimately contain a
line that does correspond to their own ledger entry. Scoring every residual-intent line as "should
not match" would understate the matcher against a truth the corpus never asserted. Recall on the
matchable set is reported beside precision, because precision alone can be bought by matching
nothing.

**p95 is defined, not assumed.** The matcher is one pure call over the whole corpus, so there is no
per-line latency to sample without instrumenting a shipped module. What is sampled is the
full-corpus `match` call, run 20 times in process, each duration divided by the line count, 95th
percentile of those. It is wall clock on the machine that ran it. It is not a service-level latency
and excludes ingestion, persistence and HTTP.

**Cost is computed from provider usage fields or it is not computed.** The committed cassettes carry
no `usage` block, deliberately — a synthesised recording saying `{"input_tokens": 0}` reads as "this
call was free" rather than "nobody measured this call", and a fabricated zero ends up inside a
published figure. So there is no token count to price and no cost for either model-dependent arm.
The deterministic arm's `0` is structural (it issues no request) and is labelled as such; compute
and database cost are not measured and are not claimed to be zero.

**The LLM-as-matcher arm's code path exists.** `cited_entry_ids` resolves a proposal's evidence
references back to the ledger entries it cited — §6.1 gives a proposal no field for a ledger entry,
so a cited `candidate_ledger_entry` item *is* the match assertion — and `grade_llm_pairings` grades
those pairings against the same same-scenario truth the deterministic arm is graded against. Both
are unit-tested. What is missing is a run worth grading: the synthesised cassettes cite
`evidence_ids[:1]`, which is the remittance-reference item and never a candidate entry, so there is
no pairing at all and a zero computed from it would be fabricated.

§20 expects this arm to lose on all three figures. **That expectation is written down and enforced
nowhere.** If a capture shows otherwise, the result is published unchanged.

---

## 7. Why the three-arm table is not committed

One column is wall clock, so a committed copy could not be drift-checked the way the §19 chaos
results table is: it would fail on every machine. `make eval-compare` prints it and whoever publishes
it records the command beside the table. Everything else in the table — the accuracy, the cost, and
every note — is deterministic, and a test asserts that two runs agree on all of it.

---

## 8. Live capture

`uv run python -m tests.evaluation live-eval` **refuses**, for two independent reasons, and both are
printed:

1. `LECP_LIVE_EVAL` is not set to `1`. A command that can reach a paid API is never the default and
   is never inferred from a credential being present.
2. Even with it set, this repository ships no transport that speaks HTTP (ADR-051). That is the
   property that makes every other evaluation command provably offline. A capture requires an
   operator to supply a transport explicitly, which is the point at which a person decides to spend
   money.

What a live capture would need, **by variable name only**:

```
LECP_LIVE_EVAL
CASSETTE_CAPTURE
ANTHROPIC_API_KEY
OPENAI_API_KEY
```

No value for any of those is printed by any command, asked for by any command, or read into any
committed artefact. `live-eval` is never invoked by CI and never by a test, and it is deliberately
not a `make` target — a command that can spend money should not be one tab-completion away.

---

## 9. Commands

```bash
make golden               # regenerate the golden set
make golden-check         # fail if it has drifted
make eval-verify          # the golden set's schema, and the scorer's arithmetic

make eval-gate            # the reproduction gate: replay vs committed baseline
make eval-gate-update     # deliberately rewrite the baseline
make eval-gate-verify     # prove the gate passes, and fails on ten injected regressions

make label-packet         # write the hold-out label packet (writes no label)
make label-packet-verify  # prove the packet leaks nothing and the validator refuses bad input

make eval-compare         # render §20's three-arm table
make eval-compare-verify  # prove the harness fabricates no number and gates live capture
```

None of these needs a database, a credential or a network.

---

## 10. The rules this harness is built around

From `CLAUDE.md`, restated because every design decision above follows from one of them:

- **The model never touches money.** It may propose a treatment code and a rationale, or abstain. It
  never computes, alters, derives, infers or encodes an amount, a rate, a percentage, a multiplier
  or a quantity. The deterministic calculator owns amount computation. No figure in any artefact
  here is a claim about a monetary value a model produced.
- **Never invent a metric.** Every number comes from a committed script and is reproducible. Where
  there is no run, the cell says `NOT MEASURED` and why.
- **A score carries the origin of the responses it was computed over**, and the scorer will not
  describe a synthesised run as a model measurement.
