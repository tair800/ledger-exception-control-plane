/**
 * One human decision on one exception.
 *
 * Three upstream routes behind one handler, chosen by an explicit verb: `approve` authorises the
 * treatment the model proposed, `edit` authorises a different one, `reject` authorises nothing. The
 * verb maps to a path here rather than being a flag on a single call, because that is how
 * `routes.py` separates them and for the reason it gives: a flag would have made the stricter path
 * reachable by forgetting to set it.
 *
 * **The console never names the principal.** `DecisionRequest` forbids extra fields and the actor
 * is resolved from the bearer token upstream. `requested_by` is not an exception to that — it names
 * the *other* principal, the one who asked for the edit, and §16's countersignature rule requires
 * it to differ from the authoriser.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import {
  DECISION_VERBS,
  TREATMENT_CODES,
  type DecisionRequest,
  type DecisionResponse,
  type DecisionVerb,
  type TreatmentCode,
} from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function refuse(message: string, status = 400) {
  return NextResponse.json(
    { status, message, authority: false, not_implemented: false },
    { status },
  );
}

interface DecisionBody {
  verb?: unknown;
  resolution_version?: unknown;
  approval_token?: unknown;
  treatment?: unknown;
  treatment_proposal_id?: unknown;
  requested_by?: unknown;
}

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!UUID.test(id)) return refuse("That is not an exception identifier.");

  let body: DecisionBody;
  try {
    body = (await request.json()) as DecisionBody;
  } catch {
    return refuse("The decision could not be read.");
  }

  const verb = body.verb;
  if (typeof verb !== "string" || !(DECISION_VERBS as readonly string[]).includes(verb)) {
    return refuse("A decision must be approve, edit or reject.");
  }

  // The idempotency key is claimed by the caller and returned by the API. It is validated, never
  // regenerated here: a token this handler invented would make a retried submission a *second*
  // decision, which is the duplicate the key exists to prevent.
  const approvalToken = body.approval_token;
  if (typeof approvalToken !== "string" || approvalToken.length < 8 || approvalToken.length > 64) {
    return refuse("An approval token of 8 to 64 characters is required.");
  }

  // Integer-valued, and checked as such rather than coerced: a version the console rounded into
  // shape would address a different resolution than the operator was looking at.
  const version = body.resolution_version;
  if (typeof version !== "number" || !Number.isInteger(version) || version < 1) {
    return refuse("A resolution version of 1 or greater is required.");
  }

  const treatment = body.treatment;
  if (
    treatment !== undefined &&
    treatment !== null &&
    !(typeof treatment === "string" && (TREATMENT_CODES as readonly string[]).includes(treatment))
  ) {
    return refuse("That is not a treatment this system recognises.");
  }

  const payload: DecisionRequest = {
    resolution_version: version,
    approval_token: approvalToken,
    ...(treatment === undefined || treatment === null
      ? {}
      : { treatment: treatment as TreatmentCode }),
    ...(typeof body.treatment_proposal_id === "string" && UUID.test(body.treatment_proposal_id)
      ? { treatment_proposal_id: body.treatment_proposal_id }
      : {}),
    ...(typeof body.requested_by === "string" && body.requested_by.length > 0
      ? { requested_by: body.requested_by }
      : {}),
  };

  const upstream = await callControlPlane<DecisionResponse>(
    `/api/v1/exceptions/${id}/${verb as DecisionVerb}`,
    { method: "POST", body: payload },
  );
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
