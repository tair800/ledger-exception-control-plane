/**
 * Which exceptions the fault injector would actually accept.
 *
 * **This exists because the console guessed and guessed wrong.** The control listed *undecided*
 * exceptions, reasoning that an undispatched posting must belong to one — but the injector needs an
 * approved decision and a priced adjustment, which an undecided exception is precisely without. The
 * default selection returned 409 every time, on the one screen a visitor is most likely to press.
 *
 * The queue summary carries `decided` and no dispatch state, so the console cannot derive
 * eligibility from what it already has. Rather than widen the production queue contract with a
 * demo-only field, the demo namespace publishes its own eligible set and this handler forwards it.
 *
 * Guarded like every other demo route: `501` when the connected control plane does not publish the
 * path, so a console pointed at an older instance disables the control with a reason instead of
 * failing on click.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { FaultTargetView } from "@/lib/types";

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane does not publish the fault-injection targets, so the control cannot know " +
  "which postings it may fault.";

export async function GET() {
  const capabilities = await probeCapabilities();
  if (!capabilities.demo_fault_targets) {
    return NextResponse.json(
      { status: 501, message: NOT_IMPLEMENTED_MESSAGE, authority: false, not_implemented: true },
      { status: 501 },
    );
  }

  const upstream = await callControlPlane<FaultTargetView[]>("/api/v1/demo/fault-targets");
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
