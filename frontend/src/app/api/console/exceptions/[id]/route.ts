/** Full provenance for one exception: evidence, proposal, decision, money, dispatch, audit. */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import type { ExceptionDetail } from "@/lib/types";

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
