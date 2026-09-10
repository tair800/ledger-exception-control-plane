/**
 * Put the demonstration back to its seeded state.
 *
 * **This exists because the demonstration is consumable.** The seeder leaves exactly one posting
 * awaiting its first dispatch, and the fault-injection control spends it. On the deployed public
 * demonstration that meant the centrepiece worked once, for the first visitor, and everyone after
 * that found a disabled button — found by exercising the live deployment rather than by reading
 * the code.
 *
 * Destructive, and only of invented rows: the control plane refuses this outright unless the
 * target database is named as disposable, so there is no configuration in which it deletes
 * anything real.
 *
 * `501` when the connected instance does not publish the route, like every other demo control.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { DemoResetReport } from "@/lib/types";

// The reset re-runs the whole seeding pipeline against a database that may have just woken from
// a free tier's idle suspend. It is the slowest thing the console can ask for.
export const maxDuration = 60;

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane does not publish a demonstration reset, so the seeded state cannot be " +
  "restored from here.";

export async function POST() {
  const capabilities = await probeCapabilities();
  if (!capabilities.demo_reset) {
    return NextResponse.json(
      { status: 501, message: NOT_IMPLEMENTED_MESSAGE, authority: false, not_implemented: true },
      { status: 501 },
    );
  }

  const upstream = await callControlPlane<DemoResetReport>("/api/v1/demo/reset", {
    method: "POST",
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
