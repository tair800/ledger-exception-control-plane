/** The exception queue. Every configured role may read it; only some may act on it. */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import type { ExceptionSummary } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

export async function GET(request: Request) {
  // `limit` is the only parameter forwarded, and it is re-derived from the incoming value rather
  // than passed through, so nothing a caller puts in the query string reaches the upstream URL
  // unexamined. Filtering by status and classification happens in the console, over this page:
  // the API takes no filter parameters, and inventing query names it would ignore would produce a
  // filter that silently did nothing.
  const requested = new URL(request.url).searchParams.get("limit");
  const limit = requested === "200" ? "200" : "50";

  const upstream = await callControlPlane<ExceptionSummary[]>("/api/v1/exceptions", {
    query: { limit },
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
