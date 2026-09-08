/**
 * Test fixtures. **Obviously synthetic, and unreachable from the running application.**
 *
 * Nothing here is imported by any module under `src/app`, `src/components` or `src/lib` — the
 * money-arithmetic scan and the application build both exclude `src/test`, and a component that
 * imported a fixture would be rendering invented financial data as though the control plane had
 * returned it. Every identifier below is prefixed so it cannot be mistaken for a real record, and
 * the amounts are not plausible balances.
 *
 * The mock router in `mockControlPlane` answers this app's own `/api/console` routes, not the
 * control plane's: the tests exercise the browser client and the screens, with the server-side
 * forwarder stubbed at the network boundary.
 */

import { vi } from "vitest";

import type {
  ConsoleMeta,
  ConsoleSession,
  DeadLetterView,
  DecisionResponse,
  ExceptionDetail,
  ExceptionSummary,
  RecoveryItemView,
  Role,
} from "@/lib/types";

export const FIXTURE_EXCEPTION_ID = "00000000-0000-4000-8000-000000000001";
export const FIXTURE_PROPOSAL_ID = "00000000-0000-4000-8000-0000000000a1";
export const FIXTURE_ADJUSTMENT_ID = "00000000-0000-4000-8000-0000000000b1";

export function session(role: Role | null = "controller"): ConsoleSession {
  return {
    signed_in: true,
    authority: role === null ? "unverified" : "verified",
    principal: role === null ? null : `TEST-PRINCIPAL-${role}`,
    role,
  };
}

export function meta(overrides: Partial<ConsoleMeta> = {}): ConsoleMeta {
  return {
    demo_mode: "unknown",
    version: "0.0.0-test",
    reachable: true,
    capabilities: {
      dlq_replay: false,
      demo_inject_crash: false,
      demo_fault_targets: false,
      identity: true,
      meta: false,
      request_edit: false,
    },
    ...overrides,
  };
}

export function summary(overrides: Partial<ExceptionSummary> = {}): ExceptionSummary {
  return {
    id: FIXTURE_EXCEPTION_ID,
    classification: "partial_capture",
    status: "open",
    psp_reference: "TEST-PSP-REF-0001",
    currency: "EUR",
    amount: "11.11",
    correlation_id: "TEST-CORRELATION-0001",
    has_proposal: true,
    decided: false,
    ...overrides,
  };
}

export function detail(overrides: Partial<ExceptionDetail> = {}): ExceptionDetail {
  return {
    id: FIXTURE_EXCEPTION_ID,
    classification: "partial_capture",
    status: "open",
    correlation_id: "TEST-CORRELATION-0001",
    line: {
      psp_reference: "TEST-PSP-REF-0001",
      merchant_reference: "TEST-MERCHANT-0001",
      transaction_type: "capture",
      amount: "11.11",
      currency: "EUR",
      value_date: "2026-07-01",
    },
    evidence: [
      {
        id: "00000000-0000-4000-8000-0000000000e1",
        kind: "merchant_memo",
        content: "TEST EVIDENCE: partial capture noted against the authorisation.",
        cited: true,
      },
      {
        id: "00000000-0000-4000-8000-0000000000e2",
        kind: "support_ticket_note",
        content: "TEST EVIDENCE: no customer contact on record.",
        cited: false,
      },
    ],
    proposal: {
      id: FIXTURE_PROPOSAL_ID,
      treatment: "rebook",
      confidence: "medium",
      rationale: "TEST RATIONALE: the memo describes a partial capture.",
      abstained: false,
      model_id: "test-model",
      model_version: "test-version",
    },
    approval: null,
    adjustment: null,
    outbox: null,
    attempts: [],
    audit: [
      {
        id: "00000000-0000-4000-8000-0000000000f1",
        occurred_at: "2026-07-01T00:00:00+00:00",
        principal: "TEST-PRINCIPAL-system",
        agent_identity: null,
        tool: "propose_treatment",
        scope_granted: "propose:treatment",
        approval_decision: "n_a",
        approver: null,
        model: "test-model",
        region_jurisdiction: null,
        outcome: "success",
        correlation_id: "TEST-CORRELATION-0001",
      },
    ],
    ...overrides,
  };
}

/** A decided exception, with the deterministic adjustment and the dispatch that followed. */
export function decidedDetail(): ExceptionDetail {
  return detail({
    status: "resolved",
    approval: {
      id: "00000000-0000-4000-8000-0000000000c1",
      decision: "approved",
      approved_treatment: "rebook",
      principal: "TEST-PRINCIPAL-controller",
      requested_by: null,
      decided_at: "2026-07-01T00:05:00+00:00",
    },
    adjustment: {
      id: FIXTURE_ADJUSTMENT_ID,
      amount: "11.11",
      currency: "EUR",
      account_code: "TEST-ACCOUNT-0001",
      period: "2026-07",
      operation_id: "TEST-OPERATION-ID-0001",
      posting_ref: "TEST-POSTING-REF-0001",
    },
    outbox: { state: "settled", last_outcome: "confirmed", attempt_count: 1 },
    attempts: [
      {
        attempt_no: 1,
        state: "resolved",
        outcome: "confirmed",
        sent_at: "2026-07-01T00:06:00+00:00",
        posting_ref: "TEST-POSTING-REF-0001",
      },
    ],
  });
}

export function decision(): DecisionResponse {
  return {
    approval_id: "00000000-0000-4000-8000-0000000000c1",
    exception_id: FIXTURE_EXCEPTION_ID,
    resolution_version: 1,
    decision: "approved",
    approved_treatment: "rebook",
    principal: "TEST-PRINCIPAL-controller",
    approval_token: "TEST-APPROVAL-TOKEN-0001",
  };
}

export function deadLetter(): DeadLetterView {
  return {
    id: "00000000-0000-4000-8000-0000000000d1",
    outbox_id: "00000000-0000-4000-8000-0000000000d2",
    adjustment_id: FIXTURE_ADJUSTMENT_ID,
    operation_id: "TEST-OPERATION-ID-0001",
    reason: "connect_timeout",
    attempts: 5,
    replay_state: "pending",
    created_at: "2026-07-01T00:10:00+00:00",
    replayed_at: null,
  };
}

export function recoveryItem(): RecoveryItemView {
  return {
    id: "00000000-0000-4000-8000-0000000000a2",
    adjustment_id: FIXTURE_ADJUSTMENT_ID,
    operation_id: "TEST-OPERATION-ID-0001",
    reason: "ambiguous_outcome",
    evidence_procedure: "TEST PROCEDURE: query the ledger for the operation identifier.",
    opened_at: "2026-07-01T00:11:00+00:00",
    sla_due_at: "2026-07-02T00:11:00+00:00",
    approving_principal: "TEST-PRINCIPAL-controller",
    overdue: false,
  };
}

export interface Recorded {
  url: string;
  method: string;
  body: unknown;
}

export type Route = { status?: number; body: unknown } | (() => { status?: number; body: unknown });

/**
 * Install a `fetch` that answers the console's own routes from a table.
 *
 * A route absent from the table is a test failure rather than a 404: a screen that quietly calls an
 * endpoint nobody declared is exactly the drift these tests exist to catch.
 */
export function mockControlPlane(routes: Record<string, Route>) {
  const recorded: Recorded[] = [];

  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input.toString();
    const method = (init?.method ?? "GET").toUpperCase();
    const key = `${method} ${url}`;
    recorded.push({
      url,
      method,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : null,
    });

    const route = routes[key];
    if (route === undefined) throw new Error(`no fixture route for ${key}`);
    const resolved = typeof route === "function" ? route() : route;
    const status = resolved.status ?? 200;

    return {
      ok: status >= 200 && status < 300,
      status,
      json: async () => resolved.body,
    } as Response;
  });

  vi.stubGlobal("fetch", fetchMock);
  return { recorded, fetchMock };
}

/** The two routes every screen needs before it renders anything of its own. */
export function sessionRoutes(role: Role | null = "controller", overrides: Partial<ConsoleMeta> = {}) {
  return {
    "GET /api/console/session": { body: session(role) },
    "GET /api/console/meta": { body: meta(overrides) },
  };
}

function respond(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

/**
 * A `fetch` that resolves the session and then leaves every other request pending forever.
 *
 * This is how the loading states are asserted: the screen has a session, so it has rendered, and
 * its own request is genuinely in flight rather than merely slow.
 */
export function mockPendingAfterSession(
  role: Role | null = "controller",
  overrides: Partial<ConsoleMeta> = {},
) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input.toString();
    if (url === "/api/console/session") return respond(session(role));
    if (url === "/api/console/meta") return respond(meta(overrides));
    return new Promise<Response>(() => {});
  });

  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
