# Operations console

The browser surface for the ledger exception control plane: the queue of settlement exceptions, the
full provenance of any one of them, the human gate, and the two operations queues.

It exists to make one thing visible in under a minute — **which part of a financial decision a
language model touched, and which part it did not**. The model proposes a treatment code and a
confidence band. Everything with a number in it is computed elsewhere, deterministically, and this
console renders those numbers as the API returned them.

---

## Rules this console holds itself to

1. **No arithmetic on money, anywhere.** Every amount is the string the control plane returned,
   rendered beside its currency. There is no total row, no currency conversion, no locale
   formatting, no rounding, and no percentage. `src/test/no-money-arithmetic.test.ts` scans the
   shipped source for arithmetic on money-named identifiers, for numeric coercion and for
   aggregation, and is itself checked against samples that must fail.
2. **The model's rationale is provenance for humans only.** It is displayed, labelled as
   model-generated, and read by nobody but the reviewer. No code parses it, extracts a number from
   it, or branches on its content.
3. **Confidence is a band, never a number.** Three values — low, medium, high — rendered as a label.
   There is no bar to fill proportionally, because a bar would be a number the console invented.
4. **Abstention is a state, not a gap.** A model that declined is shown as having declined.
5. **Authority is never claimed by the client.** The role comes from the control plane. Where the
   control plane cannot tell us, the console says the role is unverified rather than guessing one.
6. **Nothing invented is ever rendered as real.** Fixtures live under `src/test/`, are prefixed
   `TEST-`, and are unreachable from the application. Where an endpoint does not exist, the control
   is disabled and says why — it never reports a success it did not observe.

---

## Running it

```bash
cd frontend
npm install
cp .env.example .env.local        # then edit CONTROL_PLANE_BASE_URL if the API is not on :8000
npm run dev                       # http://localhost:3000
```

Sign in with a bearer token for a principal in the control plane's own registry — configured on the
API and named in `docs/deployment.md`, never here. The console validates the token against the
control plane and stores it in an httpOnly cookie; it is never held in browser storage and never
reaches page scripts.

The variable is deliberately not named in this file. The frontend scan forbids server-side
configuration names in anything under `frontend/`, and it caught this paragraph: a console
directory that mentions where the credential table lives is one edit away from a console that
reads it.

For the local demonstration — `make demo` then `make demo-api` from the repository root — the
registry is loaded for you and the three tokens are `demo-controller`, `demo-operator` and
`demo-analyst`. They are published on purpose and are safe only because of what they reach: a
disposable database on localhost, with demo mode on. A deployment supplies its own registry.

### Environment variables

Names only — values belong in `.env.local`, which is git-ignored.

| Name | Purpose | Default |
| --- | --- | --- |
| `CONTROL_PLANE_BASE_URL` | Base URL of the control plane, no trailing slash, no `/api/v1`. | `http://localhost:8000` |
| `CONTROL_PLANE_TIMEOUT_MS` | Bound on every upstream request. | `8000` |
| `CONSOLE_COOKIE_SECURE` | `true` when serving over HTTPS, so the session cookie is `Secure`. | `false` |

There is **no token in the environment**. A deployment-wide token would make every visitor the same
principal and would defeat the role separation the whole design rests on.

### Checks

```bash
npm run lint
npx tsc --noEmit
npm test
npm run build
```

---

## How it talks to the control plane

The browser never calls the control plane directly. Every request goes to this app's own route
handlers under `src/app/api/console/**`, which attach the bearer token server-side from the httpOnly
cookie and forward through `src/lib/server/backend.ts`.

Three reasons, in order:

1. The token stays out of client JavaScript.
2. The browser only ever talks to its own origin, so the control plane needs no CORS allowlist —
   `Settings.cors_allow_origins` is fail-closed and empty by default, which is correct for a
   bearer-authenticated API.
3. The set of upstream paths is closed. Nothing forwards a URL supplied by a caller.

### `openapi.json`

A **generated snapshot** of the control plane's own schema, committed so the contract test can run
offline. It is not hand-maintained. Regenerate it from the repository root with:

```bash
uv run python -c "import json, pathlib; from ledger_exception_control_plane.api import create_app; from ledger_exception_control_plane.config import Settings; pathlib.Path('frontend/openapi.json').write_text(json.dumps(create_app(Settings()).openapi(), indent=2, sort_keys=True) + '
', encoding='utf-8', newline='
')"
```

`sort_keys` and the explicit `newline` are both load-bearing. Without the first, FastAPI's
insertion order makes every regeneration a several-thousand-line diff of pure churn; without the
second, a shell redirect on Windows rewrites every line ending and does the same. Neither changes
the contract, and both hide the one line that did.

`src/test/contract.test.ts` asserts the hand-written types in `src/lib/types.ts` against it, field
by field. The types are hand-written on purpose: a generator would have produced the same shapes
with no assertion attached, and a renamed field would then surface as an empty cell on a financial
screen instead of as a failing build.

---

## Screens

| Route | What it covers |
| --- | --- |
| `/` | The exception queue, with filters over the loaded page. |
| `/exceptions/[id]` | Full provenance: line, evidence pack with citations, proposal, confidence band, abstention, human gate, deterministic adjustment, operation identifier, outbox and posting attempts, recovery status, audit trail. |
| `/dlq` | Dead letters, with the replay control. |
| `/recovery` | The manual-recovery queue and the operator's finding. |
| `/demo` | The fault-injection control and what the demonstration shows. |

Provenance for any exception is two clicks from the queue: click the row, read the page.

---

## What this console asks the control plane for

Every optional control is gated on the control plane **publishing the route**, read from its own
endpoint list rather than hard-coded here. That is what lets one console serve instances at
different versions: a control whose route is absent disables itself and says why, instead of
failing on click.

| Endpoint | Used for | State |
| --- | --- | --- |
| `GET /api/v1/me` | The signed-in principal and the authority the server will actually enforce. | **Shipped.** Four capability booleans, not a role name for the console to interpret. |
| `GET /api/v1/meta` | `{version, demo_mode, ledger_adapter}`. | **Shipped**, and unauthenticated. `demo_mode` is three-valued here — `true`, `false`, or `unknown` when the control plane could not be reached — because a fault control must never enable itself on a guess. |
| `POST /api/v1/dlq/{dlq_id}/replay` | Replay a dead letter. | **Shipped**, operator only. |
| `GET /api/v1/demo/fault-targets` | Which postings the injector would accept. | **Shipped**, demo mode only. The console used to derive this and derived it wrongly — see below. |
| `POST /api/v1/demo/exceptions/{exception_id}/inject-fault` | The duplicate-suppression demonstration. | **Shipped**, demo mode only, operator only. |
| `POST /api/v1/exceptions/{exception_id}/request-edit` | An analyst asking a controller to revise a treatment. | **Not implemented.** The control is disabled and carries the reason. |

The expected request and response for each is written at the top of the corresponding route handler
under `src/app/api/console/`.

### A control that guessed, and was wrong

The fault-injection screen used to populate its selector with **undecided** exceptions, reasoning
that a posting awaiting dispatch must belong to one. It is the exact inverse: the injector needs an
approved decision and a priced adjustment, which is what an undecided exception is defined by not
having. Every default selection returned 409 on the first press.

The console could not have derived the right answer — `ExceptionSummary` carries `decided` and
nothing about dispatch state. So the eligibility rule, which is a precondition of a demo-only
endpoint, is published by the demo namespace and read rather than inferred. The alternative was to
put dispatch state on the production queue contract to serve a demonstration, which is the wrong
direction.

### Backend observations

Noted while building against the API, and out of scope for this directory to fix:

- `ExceptionDetail.approval` omits `resolution_version`, so the console cannot address a superseding
  decision — it shows the recorded decision and offers no control. Adding the field would let a
  controller supersede from the console.
- `ExceptionDetail` carries no recovery reference. The console matches `adjustment.id` against the
  open recovery queue instead, which cannot distinguish "never in recovery" from "already resolved";
  it says so rather than reporting the stronger of the two.
- **Fixed, and recorded here because the console found it.** `POST /exceptions/{id}/approve` once
  accepted an analyst token — `APPROVAL_ROLES` held both `ANALYST` and `CONTROLLER` — while
  `ADR-056`'s own table says an analyst may reject but never authorise. Building this console
  against the documented rule is what surfaced the contradiction. Recording a decision and
  authorising a posting are now separate rights (`may_record_decision`, `may_authorise`), enforced
  on the decision path and asserted in both directions; see ADR-061.
- `ExceptionDetail.proposal`, `approval`, `adjustment` and `outbox` are open string maps rather than
  declared models, so their fields do not appear in the OpenAPI document and the contract test
  cannot check them. Typed models would make the whole detail read verifiable.

---

## Dependencies

Next.js, React and TypeScript, as the plan names, plus Tailwind for styling. Everything else is test
tooling: Vitest, Testing Library and jsdom. No component library, no state-management library, no
charting library, no design system — the console has five screens and React state is enough.

Two advisories remain against Next 15's own bundled `postcss`, closable only by moving to Next 16.
That is a major upgrade and a decision for the repository rather than for this directory.
