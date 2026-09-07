/**
 * What the *connected* control plane can actually do.
 *
 * The console does not assume what the control plane can do — it *asks*. FastAPI publishes its
 * own `/openapi.json`, and this module reads the path list from it. The lazy options were both
 * wrong: hard-code the buttons as disabled forever, or render them and let the click fail.
 *
 * Three of these were unimplemented when this console was written and have since shipped —
 * replaying a dead letter, injecting a fault in demo mode, and asking who the bearer token belongs
 * to. `request_edit` has not, and stays honestly disabled. The probe is what made that transition
 * cost nothing: the paths below were corrected to the ones the server actually publishes and the
 * controls turned themselves on.
 *
 * The consequence is worth stating: when the endpoints below are implemented, the console enables
 * their controls with no frontend change. Until then a disabled control carries the reason, which
 * is the difference between an interface that is honest about a gap and one that pretends.
 *
 * An unreachable or unparseable document leaves every capability `false`. Fail-closed: a control
 * that might not exist stays disabled.
 */

import { callControlPlane } from "@/lib/server/backend";

export interface ControlPlaneCapabilities {
  /** `POST /api/v1/dlq/{dlq_id}/replay` — an operator re-sends a dead-lettered dispatch. */
  dlq_replay: boolean;
  /** `POST /api/v1/demo/exceptions/{exception_id}/inject-fault` — demo-mode fault injection. */
  demo_inject_crash: boolean;
  /** `GET /api/v1/me` — the identity of the bearer token's principal, and its role. */
  identity: boolean;
  /** `GET /api/v1/meta` — `{demo_mode, version}`. */
  meta: boolean;
  /** `POST /api/v1/exceptions/{exception_id}/request-edit` — an analyst's edit request. */
  request_edit: boolean;
}

export const NO_CAPABILITIES: ControlPlaneCapabilities = {
  dlq_replay: false,
  demo_inject_crash: false,
  identity: false,
  meta: false,
  request_edit: false,
};

/** Paths whose presence in the document means the capability exists. Exact, templated form. */
const CAPABILITY_PATHS: Record<keyof ControlPlaneCapabilities, string> = {
  dlq_replay: "/api/v1/dlq/{dlq_id}/replay",
  demo_inject_crash: "/api/v1/demo/exceptions/{exception_id}/inject-fault",
  identity: "/api/v1/me",
  meta: "/api/v1/meta",
  request_edit: "/api/v1/exceptions/{exception_id}/request-edit",
};

interface OpenApiDocument {
  paths?: Record<string, unknown>;
}

export function capabilitiesFromDocument(document: unknown): ControlPlaneCapabilities {
  const paths = (document as OpenApiDocument | null)?.paths;
  if (typeof paths !== "object" || paths === null) return { ...NO_CAPABILITIES };

  const present = new Set(Object.keys(paths));
  const resolved = { ...NO_CAPABILITIES };
  for (const [capability, path] of Object.entries(CAPABILITY_PATHS)) {
    resolved[capability as keyof ControlPlaneCapabilities] = present.has(path);
  }
  return resolved;
}

export async function probeCapabilities(): Promise<ControlPlaneCapabilities> {
  const document = await callControlPlane<unknown>("/openapi.json", { anonymous: true });
  return document.ok ? capabilitiesFromDocument(document.data) : { ...NO_CAPABILITIES };
}
