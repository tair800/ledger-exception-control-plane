/**
 * Replay one dead-lettered dispatch.
 *
 * The control plane implements this at `POST /api/v1/dlq/{dlq_id}/replay`. This handler forwards
 * only when the connected control plane publishes that path, and otherwise answers `501` with the
 * contract — so a console pointed at an older deployment degrades honestly instead of failing on
 * click.
 *
 * It does **not** fabricate a success. A console that reported a replay it never requested would be
 * lying about a financial dispatch, which is worse than a disabled button by a wide margin.
 *
 * The contract:
 *
 *     POST /api/v1/dlq/{dlq_id}/replay
 *     auth   operator only (the queue is operator work; an approver may not replay their own posting)
 *     body   none — see below
 *     200    ReplayReportView {dlq_id, adjustment_id, operation_id, outcome, posting_ref,
 *                              detail, resolved}
 *     403    {"detail": "a replay is an operator action"}
 *
 * **No idempotency key, deliberately.** An earlier version of this handler sent a caller-claimed
 * `replay_token`. The control plane refuses that design: a second POST is a second *order*, and
 * what stops it duplicating a financial effect is the retry-independent operation identifier and
 * the ledger's declared capability — not an HTTP request cache. §13 keeps financial guarantees out
 * of transport plumbing, and a token here would have put one there.
 *
 * The response reports what the ledger answered, so the console re-renders from the answer rather
 * than assuming what changed. `resolved` is 4.3's own definition of closure, derived there.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { ReplayReportView } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane publishes no replay endpoint. Replay is available from the command " +
  "line (the 4.3 replay CLI). Upgrade the control plane to enable it here.";

export async function POST(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!UUID.test(id)) {
    return NextResponse.json(
      {
        status: 400,
        message: "That is not a dead-letter identifier.",
        authority: false,
        not_implemented: false,
      },
      { status: 400 },
    );
  }

  const capabilities = await probeCapabilities();
  if (!capabilities.dlq_replay) {
    return NextResponse.json(
      { status: 501, message: NOT_IMPLEMENTED_MESSAGE, authority: false, not_implemented: true },
      { status: 501 },
    );
  }

  // **No request body, and no caller-claimed idempotency key.**
  //
  // This console originally sent a `replay_token`, on the reasonable assumption that a re-send
  // needed one. The control plane refuses that design and says why: a second POST is a second
  // *order*, and what protects against it duplicating a financial effect is the retry-independent
  // operation identifier and the ledger's declared capability — not an HTTP request cache.
  // Accepting a token here would move a financial guarantee into browser plumbing, which
  // `PROJECT_SPEC.md` §13 says it must never live in.
  //
  // So the console sends the order and renders the answer. It does not retry on its own.
  const upstream = await callControlPlane<ReplayReportView>(`/api/v1/dlq/${id}/replay`, {
    method: "POST",
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
