/**
 * The browser's side of the console. Talks only to this app's own `/api/console` routes.
 *
 * Every call returns a discriminated result rather than throwing, because every screen has to
 * render three outcomes — loading, the answer, and a failure it can explain — and a thrown error
 * makes the third one somebody else's problem.
 */

import type {
  ApiFailure,
  ConsoleMeta,
  ConsoleSession,
  DeadLetterView,
  DecisionResponse,
  DecisionVerb,
  ExceptionDetail,
  DemoResetReport,
  ExceptionSummary,
  FaultTargetView,
  RecoveryItemView,
  RecoveryResolution,
  TreatmentCode,
} from "@/lib/types";

export type Result<T> = { ok: true; data: T } | { ok: false; failure: ApiFailure };

function transportFailure(detail: string): ApiFailure {
  return {
    status: 0,
    message: `The console could not reach its own server (${detail}).`,
    authority: false,
    not_implemented: false,
  };
}

function unexplained(status: number): ApiFailure {
  return {
    status,
    message: `The request failed (${status}).`,
    authority: status === 401 || status === 403,
    not_implemented: status === 501,
  };
}

async function request<T>(path: string, init?: RequestInit): Promise<Result<T>> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: {
        Accept: "application/json",
        ...(init?.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...init?.headers,
      },
    });
  } catch (error) {
    return { ok: false, failure: transportFailure(error instanceof Error ? error.name : "network") };
  }

  let parsed: unknown = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }

  if (!response.ok) {
    const failure =
      typeof parsed === "object" && parsed !== null && typeof (parsed as ApiFailure).message === "string"
        ? (parsed as ApiFailure)
        : unexplained(response.status);
    return { ok: false, failure };
  }
  return { ok: true, data: parsed as T };
}

// ---------------------------------------------------------------------------------------------
// Session and instance
// ---------------------------------------------------------------------------------------------

export function readSession(): Promise<Result<ConsoleSession>> {
  return request<ConsoleSession>("/api/console/session");
}

export function signIn(token: string): Promise<Result<ConsoleSession>> {
  return request<ConsoleSession>("/api/console/session", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export function signOut(): Promise<Result<ConsoleSession>> {
  return request<ConsoleSession>("/api/console/session", { method: "DELETE" });
}

export function readMeta(): Promise<Result<ConsoleMeta>> {
  return request<ConsoleMeta>("/api/console/meta");
}

// ---------------------------------------------------------------------------------------------
// Exceptions
// ---------------------------------------------------------------------------------------------

export function listExceptions(): Promise<Result<ExceptionSummary[]>> {
  return request<ExceptionSummary[]>("/api/console/exceptions?limit=200");
}

export function readException(id: string): Promise<Result<ExceptionDetail>> {
  return request<ExceptionDetail>(`/api/console/exceptions/${id}`);
}

/**
 * Claim an idempotency key for one decision.
 *
 * Claimed in the browser, at the moment the operator commits, and sent unchanged: the API returns
 * it and refuses a second use, so a double-submitted form records one decision rather than two.
 * `randomUUID` is 36 characters, inside the 8–64 the API accepts.
 */
export function claimApprovalToken(): string {
  return crypto.randomUUID();
}

export function submitDecision(
  exceptionId: string,
  decision: {
    verb: DecisionVerb;
    resolution_version: number;
    approval_token: string;
    treatment?: TreatmentCode;
    treatment_proposal_id?: string;
    requested_by?: string;
  },
): Promise<Result<DecisionResponse>> {
  return request<DecisionResponse>(`/api/console/exceptions/${exceptionId}/decision`, {
    method: "POST",
    body: JSON.stringify(decision),
  });
}

// ---------------------------------------------------------------------------------------------
// Operations queues
// ---------------------------------------------------------------------------------------------

export function listDeadLetters(pendingOnly: boolean): Promise<Result<DeadLetterView[]>> {
  return request<DeadLetterView[]>(`/api/console/dlq?pending_only=${pendingOnly ? "true" : "false"}`);
}

export function replayDeadLetter(id: string): Promise<Result<DeadLetterView>> {
  return request<DeadLetterView>(`/api/console/dlq/${id}/replay`, {
    method: "POST",
    body: JSON.stringify({ replay_token: crypto.randomUUID() }),
  });
}

export function listRecovery(staleOnly: boolean): Promise<Result<RecoveryItemView[]>> {
  return request<RecoveryItemView[]>(`/api/console/recovery?stale=${staleOnly ? "true" : "false"}`);
}

export function resolveRecovery(
  id: string,
  resolution: RecoveryResolution,
  postingRef?: string,
): Promise<Result<RecoveryItemView>> {
  return request<RecoveryItemView>(`/api/console/recovery/${id}/resolve`, {
    method: "POST",
    body: JSON.stringify({ resolution, posting_ref: postingRef ?? null }),
  });
}

/**
 * Which exceptions the fault injector would accept.
 *
 * Asked rather than inferred: eligibility is "approved, priced, and still awaiting a first
 * dispatch", and the queue summary carries none of those three.
 */
export function listFaultTargets(): Promise<Result<FaultTargetView[]>> {
  return request<FaultTargetView[]>("/api/console/demo/fault-targets");
}

/** Put the demonstration back to its seeded state, so the fault control has a target again. */
export function resetDemonstration(): Promise<Result<DemoResetReport>> {
  return request<DemoResetReport>("/api/console/demo/reset", { method: "POST" });
}

/** The demo-mode fault injector. Answers 501 until the control plane publishes the endpoint. */
export function injectCrash(exceptionId: string): Promise<Result<unknown>> {
  return request<unknown>("/api/console/demo/inject-crash", {
    method: "POST",
    body: JSON.stringify({ exception_id: exceptionId }),
  });
}
