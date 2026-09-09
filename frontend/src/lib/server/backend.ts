/**
 * The one place the console talks to the control plane.
 *
 * Everything the browser sees is served by this app's own route handlers under
 * `/api/console/**`, and they all come through here. Three reasons, in order of importance:
 *
 * 1. **The bearer token never reaches client JavaScript.** It arrives once, at sign-in, and is kept
 *    in an httpOnly cookie. This module reads that cookie server-side and attaches the header. A
 *    console that held the token in `localStorage` would put a credential for authorising financial
 *    writes inside reach of any script on the page.
 * 2. **Same-origin.** `Settings.cors_allow_origins` is fail-closed and empty by default, which is
 *    the right default for a bearer-authenticated API. Proxying means the console works against it
 *    without an origin allowlist at all.
 * 3. **The upstream path list is closed.** Callers name a path; nothing forwards an arbitrary URL,
 *    so this is not a general-purpose proxy an attacker could aim somewhere else.
 *
 * It is deliberately not a caching or retry layer. A retry here would be a second delivery attempt
 * for a decision the operator made once, and the decision endpoints are made safe by the
 * `approval_token` idempotency key rather than by anything this file could do.
 */

import { cookies } from "next/headers";

import { controlPlaneBaseUrl, upstreamTimeoutMs } from "@/lib/server/config";
import type { ApiFailure } from "@/lib/types";

/** The cookie holding the operator's bearer token. Never readable from client JavaScript. */
export const SESSION_COOKIE = "lecp_console_token";

export async function sessionToken(): Promise<string | null> {
  const jar = await cookies();
  const value = jar.get(SESSION_COOKIE)?.value;
  return value && value.length > 0 ? value : null;
}

/** The result of one upstream call: either a parsed body, or a failure the console can explain. */
export type Upstream<T> = { ok: true; status: number; data: T } | { ok: false; failure: ApiFailure };

/**
 * Turn an upstream refusal into something the console can render.
 *
 * The upstream body is *not* passed through verbatim. `routes.py` sends `{"reason": ...}` for
 * refusals it wants a caller to understand, and that reason is kept; anything else collapses to a
 * status-shaped sentence, because forwarding an arbitrary upstream body into the page is how a
 * stack trace ends up on a reviewer's screen.
 */
function failureFor(status: number, body: unknown): ApiFailure {
  const reason = readReason(body);
  const authority = status === 401 || status === 403;
  const notImplemented = status === 404 || status === 501;

  return {
    status,
    reason,
    authority,
    not_implemented: status === 501,
    message: authority
      ? authorityMessage(status, reason)
      : notImplemented && status === 501
        ? "The connected control plane does not implement this endpoint yet."
        : (REFUSAL_MESSAGES[reason ?? ""] ?? statusMessage(status)),
  };
}

function readReason(body: unknown): string | undefined {
  if (typeof body !== "object" || body === null) return undefined;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "object" && detail !== null) {
    const reason = (detail as { reason?: unknown }).reason;
    if (typeof reason === "string") return reason;
  }
  return undefined;
}

function authorityMessage(status: number, reason: string | undefined): string {
  if (status === 401) {
    return "This control plane did not recognise the bearer token. Sign in again.";
  }
  return (
    REFUSAL_MESSAGES[reason ?? ""] ??
    "Your role does not hold the authority for this action. Authority is decided by the control plane, not by this console."
  );
}

/**
 * A sentence per refusal reason the API defines, written for the person who hit it.
 *
 * Kept here rather than in the components because a refusal is part of the API contract: the same
 * reason means the same thing wherever it surfaces, and two components paraphrasing it differently
 * would be two different explanations of one rule.
 */
const REFUSAL_MESSAGES: Record<string, string> = {
  role_may_not_approve: "Your role may not record an approval decision.",
  role_may_not_edit: "Your role may request a different treatment but may not authorise one.",
  self_countersigned_edit:
    "An edited treatment must be authorised by a principal other than the one who requested it.",
  token_already_used: "That approval token has already been consumed. Reload and decide once.",
  already_decided: "This exception has already been decided at this resolution version.",
  treatment_inconsistent_with_decision:
    "The decision and the treatment it names are inconsistent — a rejection authorises nothing.",
  unknown_subject: "The control plane has no record of that subject.",
  supersession_blocked:
    "An operation on this exception is in flight, ambiguous, or open in recovery. One exception must never have two live resolutions.",
  role_may_not_recover: "The dead-letter and recovery queues are worked by the operator role.",
  approver_may_not_resolve:
    "The principal who authorised a posting may not also judge what happened to it.",
  already_resolved: "That recovery item has already been resolved.",
  confirmed_without_reference:
    "Confirming by evidence requires the posting reference that evidences it.",
  reference_without_confirmation:
    "A posting reference is evidence of confirmation and may not accompany any other resolution.",
  unknown_item: "The control plane has no record of that recovery item.",
};

function statusMessage(status: number): string {
  if (status === 404) return "The control plane has no record of that.";
  if (status === 409) return "That action conflicts with the current state. Reload and try again.";
  if (status === 422) return "The control plane refused the request as inconsistent.";
  if (status >= 500) return "The control plane failed while handling the request.";
  return `The control plane refused the request (${status}).`;
}

/**
 * A transport failure: the console reached nothing, so it has nothing to explain.
 *
 * **A timeout is told apart from an outage, and the reason is the deployed demonstration.** The
 * control plane runs on a free tier that scales to zero, so the first request after an idle period
 * waits for a container to start — tens of seconds. That is not a broken backend, and telling a
 * visitor to "check that it is running and that CONTROL_PLANE_BASE_URL is correct" would be
 * developer instructions offered to somebody who cannot act on them and a wrong diagnosis besides.
 *
 * `AbortSignal.timeout()` rejects with a `TimeoutError`, which is what makes the two separable at
 * all. Every other transport failure keeps the original wording: for those, the configuration
 * really is the first thing to check.
 */
export function unreachable(detail: string): ApiFailure {
  const timedOut = detail === "TimeoutError";
  return {
    status: 0,
    message: timedOut
      ? "The control plane did not answer in time. It runs on a free tier that sleeps when idle, so the first request after a quiet period can take up to a minute while it starts. Try again in a moment."
      : `The control plane could not be reached (${detail}). Check that it is running and that CONTROL_PLANE_BASE_URL is correct.`,
    authority: false,
    not_implemented: false,
  };
}

export function unauthenticated(): ApiFailure {
  return {
    status: 401,
    message: "Sign in with a control-plane bearer token to use the console.",
    authority: true,
    not_implemented: false,
  };
}

/**
 * Call the control plane with the signed-in principal's token.
 *
 * `path` is appended to the base URL as given, so a caller passes `/api/v1/exceptions` or
 * `/healthz`. It is never taken from a request body or a query string by any caller in this app.
 */
export async function callControlPlane<T>(
  path: string,
  init: {
    method?: "GET" | "POST";
    body?: unknown;
    query?: Record<string, string>;
    /** Use this token instead of the cookie — for validating one at sign-in, before it is set. */
    token?: string;
    /** `/healthz` and `/openapi.json` take no principal. Nothing under `/api/v1` may set this. */
    anonymous?: boolean;
  } = {},
): Promise<Upstream<T>> {
  const token = init.anonymous ? null : (init.token ?? (await sessionToken()));
  if (token === null && !init.anonymous) return { ok: false, failure: unauthenticated() };

  const url = new URL(`${controlPlaneBaseUrl()}${path}`);
  for (const [key, value] of Object.entries(init.query ?? {})) {
    url.searchParams.set(key, value);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method: init.method ?? "GET",
      headers: {
        ...(token === null ? {} : { Authorization: `Bearer ${token}` }),
        Accept: "application/json",
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: init.body === undefined ? undefined : JSON.stringify(init.body),
      cache: "no-store",
      signal: AbortSignal.timeout(upstreamTimeoutMs()),
    });
  } catch (error) {
    return {
      ok: false,
      failure: unreachable(error instanceof Error ? error.name : "network error"),
    };
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text.length > 0) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) return { ok: false, failure: failureFor(response.status, parsed) };
  return { ok: true, status: response.status, data: parsed as T };
}
