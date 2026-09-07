/**
 * Record what an operator found about an ambiguous dispatch. **This never causes a posting.**
 *
 * `posting_ref` is required by `confirmed_by_evidence` and refused by every other resolution — it
 * is the evidence, not a note. The check is upstream, where the refusal can say why; the console
 * only keeps the field from being sent where it has no meaning.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { RECOVERY_RESOLUTIONS, type RecoveryItemView, type ResolveRequest } from "@/lib/types";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function refuse(message: string) {
  return NextResponse.json(
    { status: 400, message, authority: false, not_implemented: false },
    { status: 400 },
  );
}

export async function POST(request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!UUID.test(id)) return refuse("That is not a recovery identifier.");

  let body: { resolution?: unknown; posting_ref?: unknown };
  try {
    body = (await request.json()) as { resolution?: unknown; posting_ref?: unknown };
  } catch {
    return refuse("The resolution could not be read.");
  }

  const resolution = body.resolution;
  if (
    typeof resolution !== "string" ||
    !(RECOVERY_RESOLUTIONS as readonly string[]).includes(resolution)
  ) {
    return refuse("That is not a resolution this system recognises.");
  }

  const postingRef = body.posting_ref;
  const payload: ResolveRequest = {
    resolution: resolution as ResolveRequest["resolution"],
    ...(typeof postingRef === "string" && postingRef.trim().length > 0
      ? { posting_ref: postingRef.trim() }
      : {}),
  };

  const upstream = await callControlPlane<RecoveryItemView>(`/api/v1/recovery/${id}/resolve`, {
    method: "POST",
    body: payload,
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
