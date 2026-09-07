/**
 * The dead-letter queue: what exhausted its retry budget, and why.
 *
 * The upstream refuses a non-operator rather than filtering, deliberately — an analyst is told they
 * lack the authority instead of being shown an empty queue and left to conclude nothing failed. The
 * console preserves that: a 403 renders as an authority message, never as "no dead letters".
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import type { DeadLetterView } from "@/lib/types";

export async function GET(request: Request) {
  const pendingOnly = new URL(request.url).searchParams.get("pending_only") !== "false";

  const upstream = await callControlPlane<DeadLetterView[]>("/api/v1/dlq", {
    query: { pending_only: pendingOnly ? "true" : "false" },
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
