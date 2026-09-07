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

Sign in with a bearer token for a principal configured in the control plane's `PRINCIPALS`
registry. The console validates it against `GET /api/v1/exceptions` and stores it in an httpOnly
cookie; it is never held in browser storage and never reaches page scripts.

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
uv run python -c "import json; from ledger_exception_control_plane.api import create_app; from ledger_exception_control_plane.config import Settings; print(json.dumps(create_app(Settings()).openapi(), indent=2))" > frontend/openapi.json
```

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

## What this console needs and does not have

Four endpoints are specified and not implemented on the control plane. The console **asks** the
control plane which of them exist — it reads the published endpoint list — rather than hard-coding
the answer, so each control enables itself when its route is built, with no change here. Until then
the control is disabled and carries the reason.

| Needed | Used for | Behaviour today |
| --- | --- | --- |
| `GET /api/v1/me` | The signed-in principal and role, so a control the role may not use is not rendered. | The console shows a persistent "role unverified" banner, renders the documented controls, and reports a 403 as an authority message. |
| `GET /api/v1/meta` | `{demo_mode, version}`. | `demo_mode` is reported as **unknown**, distinct from `false`; the fault-injection control stays disabled. `version` is read from `/healthz` instead. |
| `POST /api/v1/dlq/{dead_letter_id}/replay` | Replay from the console. | The replay button is disabled with an explanation. No replay is ever reported as having happened. |
| `POST /api/v1/demo/inject-crash` | The live duplicate-suppression demonstration. | The control is disabled outside demo mode and while the endpoint is absent. |

The expected request and response for each is written at the top of the corresponding route handler
under `src/app/api/console/`.

### Backend observations

Noted while building against the API, and out of scope for this directory to fix:

- `ExceptionDetail.approval` omits `resolution_version`, so the console cannot address a superseding
  decision — it shows the recorded decision and offers no control. Adding the field would let a
  controller supersede from the console.
- `ExceptionDetail` carries no recovery reference. The console matches `adjustment.id` against the
  open recovery queue instead, which cannot distinguish "never in recovery" from "already resolved";
  it says so rather than reporting the stronger of the two.
- `POST /exceptions/{id}/approve` accepts an **analyst** token: `APPROVAL_ROLES` contains both
  `ANALYST` and `CONTROLLER`, and only the `edit` route narrows to `EDIT_ROLES`. The documented rule
  is that an analyst may reject and request an edit but never authorise. The console renders the
  documented rule and does not offer an analyst the approve control.
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
