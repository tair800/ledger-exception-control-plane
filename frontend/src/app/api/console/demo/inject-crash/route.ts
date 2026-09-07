/**
 * Demo mode: crash a dispatch between the socket write and the response, then let the system
 * recover, so a visitor can watch duplicate suppression happen rather than read that it was tested.
 *
 * **The control plane does not implement this yet**, and this handler will not pretend otherwise: it
 * forwards only when the connected instance publishes the path, and otherwise answers `501`. The
 * button in the UI is disabled with the same explanation. Faking the outcome would be the worst
 * possible thing to fake — the whole demonstration is that the *system's* behaviour is trustworthy.
 *
 * The contract this expects, for whoever implements it:
 *
 *     POST /api/v1/demo/inject-crash
 *     auth   operator only, and refused outright unless `Settings.demo_mode` is true (§16: "the
 *            demo fault-injection endpoint is disabled unless demo mode is explicitly configured")
 *     body   {"exception_id": "<uuid>", "fault": "crash_after_socket_write"}
 *     202    {
 *              "correlation_id": "...",
 *              "operation_id": "...",
 *              "fault": "crash_after_socket_write",
 *              "attempts": [{"attempt_no": 1, "state": "...", "outcome": "..."}],
 *              "ledger_applied_count": 1,
 *              "outbox_state": "settled",
 *              "duplicate_suppressed": true
 *            }
 *     403    {"detail": {"reason": "demo_mode_disabled"}} | {"reason": "role_may_not_recover"}
 *     409    {"detail": {"reason": "supersession_blocked"}}
 *
 * `ledger_applied_count` is the field that carries the demonstration, and it has to come from the
 * simulated ledger's own applied count — §19.1 forbids inferring an outcome from our own records.
 * A console that computed "no duplicate" from the absence of a second row in *our* tables would be
 * asserting the conclusion rather than observing it, which is exactly the defect the chaos suite
 * was written to avoid.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export const FAULT = "crash_after_socket_write";

export const NOT_IMPLEMENTED_MESSAGE =
  "This control plane has no fault-injection endpoint. The crash scenarios run in the committed " +
  "chaos suite; the demo-mode HTTP control is specified in this handler and not yet built.";

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

  const upstream = await callControlPlane<unknown>("/api/v1/demo/inject-crash", {
    method: "POST",
    body: { exception_id: exceptionId, fault: FAULT },
  });
  if (!upstream.ok) {
    return NextResponse.json(upstream.failure, { status: upstream.failure.status || 502 });
  }
  return NextResponse.json(upstream.data);
}
