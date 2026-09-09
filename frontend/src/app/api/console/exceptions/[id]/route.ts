/** Full provenance for one exception: evidence, proposal, decision, money, dispatch, audit. */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import type { ExceptionDetail } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

/** Rejected here rather than forwarded: a path segment is not a place to accept arbitrary text. */
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export async function GET(_request: Request, context: { params: Promise<{ id: string }> }) {
  const { id } = await context.params;
  if (!UUID.test(id)) {
    return NextResponse.json(
      {
        status: 400,
        message: "That is not an exception identifier.",
        authority: false,
        not_implemented: false,
      },
      { status: 400 },
    );
  }

  const upstream = await callControlPlane<ExceptionDetail>(`/api/v1/exceptions/${id}`);
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
