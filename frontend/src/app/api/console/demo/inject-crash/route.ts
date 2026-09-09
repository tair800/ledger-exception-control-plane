/**
 * Demo mode: crash a dispatch between the socket write and the response, then let the system
 * recover, so a visitor can watch duplicate suppression happen rather than read that it was tested.
 *
 * The control plane implements this at `POST /api/v1/demo/exceptions/{exception_id}/inject-fault`,
 * which answers 404 outside demo mode and refuses any principal without operator authority. This
 * handler forwards only when the connected instance publishes that path and otherwise answers
 * `501`, with the button disabled and carrying the same reason. Faking the outcome would be the
 * worst possible thing to fake — the whole demonstration is that the *system's* behaviour is
 * trustworthy.
 *
 * The fault is not a parameter. The control plane injects exactly the one §19.1 names, because a
 * menu would invite a visitor to hunt for the failure the system handles worst, and because a lost
 * response after a committed write is the case the entire reliability layer exists for.
 *
 * `ledger_applied_count` is the field that carries the demonstration, and it comes from the
 * simulated ledger's own applied count — §19.1 forbids inferring an outcome from our own records.
 * A console that computed "no duplicate" from the absence of a second row in *our* tables would be
 * asserting the conclusion rather than observing it, which is exactly the defect the chaos suite
 * was written to avoid.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { InjectedFaultReport } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane publishes no fault-injection endpoint. The crash scenarios still run in " +
  "the committed chaos suite; only the demo-mode HTTP control is absent from this instance.";

export async function POST(request: Request) {
  const capabilities = await probeCapabilities();
  if (!capabilities.demo_inject_crash) {
    return NextResponse.json(
      { status: 501, message: NOT_IMPLEMENTED_MESSAGE, authority: false, not_implemented: true },
      { status: 501 },
    );
  }

  let exceptionId: unknown;
  try {
    exceptionId = (await request.json())?.exception_id;
  } catch {
    exceptionId = undefined;
  }
  if (typeof exceptionId !== "string" || !UUID.test(exceptionId)) {
    return NextResponse.json(
      {
        status: 400,
        message: "An exception identifier is required.",
        authority: false,
        not_implemented: false,
      },
      { status: 400 },
    );
  }

  // The exception is addressed in the path and the fault is not a parameter: the control plane
  // injects exactly §19.1's lost-response fault, because that is the failure the whole reliability
  // layer exists for and a demo control that let a visitor pick from a menu would be inviting them
  // to find the one the system handles worst.
  const upstream = await callControlPlane<InjectedFaultReport>(
    `/api/v1/demo/exceptions/${exceptionId}/inject-fault`,
    { method: "POST" },
  );
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
