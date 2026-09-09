/**
 * The manual-recovery queue: where the automatic path stopped.
 *
 * These are dispatches whose outcome is ambiguous — the request was sent and the answer never
 * arrived — so the system will not retry them and will not guess. An operator inspects the ledger
 * and records what they found. Recording a finding never causes a posting.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import type { RecoveryItemView } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

export async function GET(request: Request) {
  const stale = new URL(request.url).searchParams.get("stale") === "true";

  const upstream = await callControlPlane<RecoveryItemView[]>("/api/v1/recovery", {
    query: { stale: stale ? "true" : "false" },
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
