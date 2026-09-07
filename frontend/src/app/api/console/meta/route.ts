/**
 * What the console knows about the instance it is connected to.
 *
 * `version` comes from `/healthz`, which takes no principal and already carries it. `demo_mode`
 * comes from `GET /api/v1/meta` where that exists, and is reported as `"unknown"` where it does not
 * — the fault-injection control needs to distinguish "this is not a demo instance" from "nobody has
 * told me", and defaulting to `false` would collapse the two.
 */

import { NextResponse } from "next/server";

import { callControlPlane } from "@/lib/server/backend";
import { probeCapabilities } from "@/lib/server/capabilities";
import type { ConsoleMeta } from "@/lib/types";

interface Liveness {
  status?: unknown;
  service?: unknown;
  version?: unknown;
}

interface MetaResponse {
  demo_mode?: unknown;
  version?: unknown;
}

export async function GET() {
  const capabilities = await probeCapabilities();
  const liveness = await callControlPlane<Liveness>("/healthz", { anonymous: true });

  let demoMode: boolean | "unknown" = "unknown";
  if (capabilities.meta) {
    const meta = await callControlPlane<MetaResponse>("/api/v1/meta");
    if (meta.ok && typeof meta.data.demo_mode === "boolean") demoMode = meta.data.demo_mode;
  }

  const body: ConsoleMeta = {
    demo_mode: demoMode,
    version: liveness.ok && typeof liveness.data.version === "string" ? liveness.data.version : null,
    reachable: liveness.ok,
    capabilities,
  };
  return NextResponse.json(body);
}
