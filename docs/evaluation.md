# Evaluation (`PROJECT_SPEC.md` §20)

What is measured, what is not, and why the difference is enforced by code rather than by a caveat.

Read this before quoting any number out of `tests/golden/`.

---

## 1. The one-paragraph version

The deterministic layers of this system are measured, and since 6.4 so is the model layer —
**section 8 has the numbers, and they are not flattering**.

**The shipped package still cannot make a call**, and that is the property everything else rests
on: no provider SDK is in the dependency graph, and a guard test walks `src/…/llm/` and fails the
build if anything there imports an HTTP client. The one transport that dials lives under `tests/`,
behind two independent opt-ins, and CI sets neither.

**Two kinds of cassette are committed, and confusing them would undo the whole harness.** The
canonical corpus is **synthesised** — written by `tests/cassette_builder.py`, never received from a
provider — so every figure computed from it is a fact about this harness. `tests/golden/live/` is
**captured**: a real provider said those words, and a figure computed from it is a fact about a
model. The harness makes the difference structural rather than editorial: `score()` takes a
required response origin with no default, `Score.headline()` refuses the word "accuracy" for a
synthesised run, and `arms.Figure` cannot hold a value without an origin or an absence without a
stated reason.

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

**These cells stayed `NOT MEASURED` after the live run of §8, and that is not an oversight.** This
arm asks a model to perform the *matching* — to pair a settlement line with a ledger entry. §8
measured a different task: proposing a *treatment* for a line the deterministic matcher already
failed to pair. A number from one is not a number for the other, and moving it across would be the
most convenient lie available.

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

## 8. The live model measurement (6.4)

**This ran.** One bounded pass, all 250 golden records, 2026-09-10. Full reasoning in **ADR-070**.

### 8.1 The result

| | |
|---|---|
| Records | 250 |
| Live calls | **251**, against a declared ceiling of 750 |
| Retries | 1 |
| Usable through the shipped path | **247 / 250 — 98.8%** |
| Refused | 3 — two truncated tool arguments, one hallucinated evidence id |
| **Accuracy, the 247 answered** | **27.9%** |
| Constant-answer baseline | 85.6% (`escalate`) |
| **Lift over baseline** | **−57.7%** |
| **Accuracy on the 36 priceable** | **97.2%** (35 of 36) |
| Abstention | 13.8% — 34 of 247, **none on a priceable case** |
| Latency | min 2.20s · p50 4.80s · p95 8.15s · max 17.97s |
| Tokens | 799,492 prompt · 46,703 completion · 846,195 total |

**Usable, not "schema-valid", and the distinction was a correction.** The first version of this
table reported 248 — the count the *adapter* accepts. The shipped path (`llm/flow.py`) also runs
`assert_citations_were_supplied`, and one answer cited an evidence id it was never shown: a
corrupted UUID. Through the pipeline it is refused, so 247 is what a pipeline reader should be
told. An adversarial review of this document caught the harness skipping that check; the harness
now runs it.

**The headline is worse than answering `escalate` every time, and it is published first.** The
breakdown says why:

| classification | records | answered | correct label | model accuracy |
|---|---|---|---|---|
| `cross_period_refund` | 12 | 12 | `accrue` | **100.0%** |
| `chargeback_reversal` | 24 | 24 | `rebook` | **95.8%** |
| `fee_split` | 72 | 71 | `escalate` | 7.0% |
| `unclassified` | 142 | 140 | `escalate` | 20.7% |

The model is good at the judgement and bad at declining to make one. Of the 211 records it answered
whose correct answer is *refer this to a human* (214 carry that label), it proposed a concrete
treatment in **177**. Every hold-out disagreement runs the same direction; not one is a wrong
answer on a priceable case.

### What actually contains that, and it is not one control

The tempting sentence is *without the approval gate this model would have posted 177 wrong
treatments*. It is wrong, an adversarial review caught it, and the truth is more useful: **three
independent fail-closed controls caught different things, and only one of them is the gate.**

1. **The citation check refused one answer outright** — the hallucinated evidence id above. That
   proposal never became a record for anybody to approve.
2. **The deterministic calculator refuses all 177.** Every one is `unclassified` (111) or
   `fee_split` (66); neither class has an account configured, so `compute_adjustment` returns
   `NO_ACCOUNT_MAPPED` before an amount exists — no instruction, no `adjustment` row, no outbox
   row, no posting. **This is not luck.** The golden set carries `label_rule:
   nothing_is_configured_for_this_class` on exactly those records: the reason the correct label is
   `escalate` and the reason the amount cannot be computed are the same fact.
3. **The approval gate stands in front of exactly one answer.** Of the 36 records where a proposal
   really would have produced a priced instruction, 35 were right and **one was wrong** — a
   chargeback reversal the model wanted to `accrue` to account 4900 instead of `rebook`. Nothing
   upstream would have caught it. A human authorising the write is the only control that sees it.

So the measurement supports a narrower claim than the first draft made, and a better one: **the
bulk failure is contained structurally, and the gate covers the residue structure cannot.** ADR-056
designed the gate from a specification clause and ADR-061 repaired a defect in it; this is the
first evidence of what it actually catches.

### 8.2 What was called, stated exactly

| | |
|---|---|
| Route | OmniRoute, OpenAI-compatible, `POST /v1/chat/completions` |
| Alias requested | `auto/best-free` — a routed alias, not a model |
| Model the route named | `gpt-5.5`, on all 250 responses |
| Upstream provider | **not independently verifiable from here, and not claimed** |
| Model version reported | `unversioned` — the alias carries no dated snapshot |

**Cost: actual marginal API cost not measured; calls were executed through the owner's
subscription-backed OmniRoute route.** No response carried a billing or cost field. A list-price
equivalent is *not* computed, because that needs the physical upstream and the row above says it is
unknown — and an estimate printed beside measured figures becomes a measured figure by proximity.

**Token counts include the router's overhead.** A one-sentence prompt through this route reported
2,024 prompt tokens before any of our content. The 3,198-token mean is what the route billed, not
the size of the evidence document.

**Temperature is not pinned**, so a re-run would not reproduce these answers token-for-token.
Scoring *is* reproducible — the captured cassette replays offline to the identical 247 proposals and
a test asserts it — and the difference is stated rather than glossed.

### 8.3 How it was kept safe

Two opt-ins, both required, neither implying the other: `LECP_LIVE_EVAL=1` says a measurement was
intended, `CASSETTE_CAPTURE=1` says a recording was. Both are construction-time refusals. **CI sets
neither**, the credential is not in CI, and `live-eval` is deliberately still not a `make` target.

- **Bounded by arithmetic.** `CallBudget` raises on the call that would exceed the ceiling. No
  adaptive sampling, no second pass, no expansion after the score was seen.
- **The prompt cannot carry the answer.** `assert_no_answer_leaks` walks all 250 prompts before the
  first call and fails the run if an answer-bearing field name — or a label's actual text — appears.
  Subjects are rebuilt from the seeded corpus by a function that never reads a golden record;
  labels are joined back on `exception_id` afterwards. The forbidden-field list is asserted against
  the dataclass, so a new label field cannot fall quietly outside it.
- **`src/` is still provably offline.** The guard that fails the build if anything under `llm/`
  imports an HTTP client was not weakened, exempted or widened. The transport is in
  `tests/evaluation/livetransport.py` — exactly the shape the cassette module described: *"recording
  wraps a transport an operator supplies and nothing here owns a socket."*
- **The synthesised cassette was not touched.** Live interactions go to their own file, stamped
  `captured` by the only class permitted to claim it.

### 8.4 One shipped change, and why it was measured before it was made

`response_format` with `strict: true` is an OpenAI *feature*, not a property of the wire format.
Probed against this route before any evaluation call: the key is accepted, 200 is returned, and the
schema is **not enforced** — the answer came back with `evidence_ids` instead of `evidence_refs` and
no `confidence`, and `validated_proposal` rejected it. The same schema as a **forced tool call** came
back exactly conformant.

Running on the existing adapter would have reported a schema-valid rate near zero: a fact about the
gateway's feature support, published as if it were a fact about a model's ability to follow a
contract. So `llm/providers/openai_tools.py` carries the identical `proposal_wire_schema()` in the
tool's `parameters`, and every answer still passes through the identical `validated_proposal`. One
definition of the contract, two envelopes, and a test asserts they are the same object.

It also sends `stream: false` explicitly: this route switches to server-sent events whenever a
request carries `tools` or `response_format` and no `stream` key.

### 8.5 Artefacts

```
tests/golden/live/live-proposals.jsonl   248 rows, scoreable by the ordinary scorer
tests/golden/live/live-cassette.json     250 interactions, origin: captured, replays offline
tests/golden/live/live-run.json          per-record latency, tokens, attempts, prompt hash
```

`tests/test_live_evaluation.py` replays the cassette through the same adapter that recorded it and
asserts every proposal comes back identical. **That test needs no network**, which is the whole
argument for where the transport lives. `live-eval --from-cassette` goes further and recomputes
every published figure from the capture — no credential, no opt-in, no call — so a reader can
check the numbers rather than take them.

### 8.6 What this does not establish

- **The public demonstration still has no model.** No provider credential is configured on any
  deployed service; the console still shows a proposal declaring itself `stand-in`.
- **The hold-out is still four independent judgements.** It reports 37.5% agreement (9 of the 24
  it answered, of 25) and 100% on its 6 priceable records — the same four class rules seen again
  (ADR-068).
- **No threshold follows from this.** OPEN-6 stays open, and now says why: one run of one routed
  alias is not a distribution, and the overall figure would gate the wrong thing.
- **The LLM-as-matcher arm is still `NOT MEASURED`.** That arm asks a model to do the *matching*,
  which is a different task from proposing a treatment.

### 8.7 Running it

```bash
LECP_LIVE_EVAL=1 CASSETTE_CAPTURE=1 \
LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... \
  uv run python -m tests.evaluation live-eval --plan-only   # plan + leakage check, no call
  uv run python -m tests.evaluation live-eval               # the bounded run
```

The variable **names** are documented in `.env.example`. No value for any of them is printed by any
command, asked for by any command, or written into any committed artefact.

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
