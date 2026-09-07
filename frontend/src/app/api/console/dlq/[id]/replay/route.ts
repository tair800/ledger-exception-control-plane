/**
 * Replay one dead-lettered dispatch.
 *
 * **The control plane does not implement this yet.** `PROJECT_SPEC.md` §10 lists
 * `POST /dlq/{id}/replay` beside `GET /dlq`, and increment 4.3 built a replay *CLI*; the HTTP route
 * has not been added. So this handler forwards only when the connected control plane publishes the
 * path, and otherwise answers `501` with the contract it is waiting for.
 *
 * It does **not** fabricate a success. A console that reported a replay it never requested would be
 * lying about a financial dispatch, which is worse than a disabled button by a wide margin. The
 * control in the UI is disabled for the same reason and says the same thing.
 *
 * The contract this expects, for whoever implements it:
 *
 *     POST /api/v1/dlq/{dead_letter_id}/replay
 *     auth   operator only (the queue is operator work; an approver may not replay their own posting)
 *     body   {"replay_token": "<8-64 chars>"}   idempotency key, claimed by the caller
 *     200    DeadLetterView with replay_state == "replayed" and replayed_at set
 *     403    {"detail": {"reason": "role_may_not_recover"}}
 *     409    {"detail": {"reason": "already_replayed"}} | {"reason": "token_already_used"}
 *     404    {"detail": {"reason": "unknown_item"}}
 *
 * The response being a `DeadLetterView` matters: the console re-renders the row from the answer
 * rather than assuming what changed, and `replay_state` is the field that proves it happened.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { DeadLetterView } from "@/lib/types";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane has no replay endpoint. Replay is available from the command line " +
  "(the 4.3 replay CLI); the HTTP route is specified in this handler and not yet built.";

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
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

  let replayToken: unknown;
  try {
    replayToken = (await request.json())?.replay_token;
  } catch {
    replayToken = undefined;
  }
  if (typeof replayToken !== "string" || replayToken.length < 8 || replayToken.length > 64) {
    return NextResponse.json(
      {
        status: 400,
        message: "A replay token of 8 to 64 characters is required.",
        authority: false,
        not_implemented: false,
      },
      { status: 400 },
    );
  }

  const upstream = await callControlPlane<DeadLetterView>(`/api/v1/dlq/${id}/replay`, {
    method: "POST",
    body: { replay_token: replayToken },
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
