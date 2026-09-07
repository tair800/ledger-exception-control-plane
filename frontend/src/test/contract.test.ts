import { readFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { capabilitiesFromDocument } from "@/lib/server/capabilities";
import { FRONTEND_ROOT } from "@/test/source-scan";
import {
  AUDIT_EVENT_VIEW_KEYS,
  CONFIDENCE_BANDS,
  DEAD_LETTER_VIEW_KEYS,
  DECISION_REQUEST_KEYS,
  DECISION_RESPONSE_KEYS,
  EVIDENCE_VIEW_KEYS,
  EXCEPTION_DETAIL_KEYS,
  EXCEPTION_SUMMARY_KEYS,
  POSTING_ATTEMPT_VIEW_KEYS,
  RECOVERY_ITEM_VIEW_KEYS,
  RECOVERY_RESOLUTIONS,
  RESOLVE_REQUEST_KEYS,
  TREATMENT_CODES,
} from "@/lib/types";

/**
 * The console's types, checked against the committed contract snapshot.
 *
 * `frontend/openapi.json` is generated from the running application's own schema — the command is
 * in `frontend/README.md` — and committed so this test has something to assert against offline. It
 * is the reason the types in `src/lib/types.ts` are hand-written: a generator would have produced
 * the same shapes with no assertion attached, and a renamed field would then have surfaced as an
 * empty cell on a financial screen rather than as a failing build.
 */

interface Schema {
  properties?: Record<string, unknown>;
  required?: string[];
  enum?: string[];
}

const document = JSON.parse(
  readFileSync(join(FRONTEND_ROOT, "openapi.json"), "utf8"),
) as {
  paths: Record<string, Record<string, unknown>>;
  components: { schemas: Record<string, Schema> };
};

const schemas = document.components.schemas;

function schema(name: string): Schema {
  const found = schemas[name];
  expect(found, `the contract snapshot has no schema named ${name}`).toBeDefined();
  return found as Schema;
}

describe("response models match the committed contract", () => {
  const models: { name: string; keys: readonly string[] }[] = [
    { name: "ExceptionSummary", keys: EXCEPTION_SUMMARY_KEYS },
    { name: "ExceptionDetail", keys: EXCEPTION_DETAIL_KEYS },
    { name: "EvidenceView", keys: EVIDENCE_VIEW_KEYS },
    { name: "PostingAttemptView", keys: POSTING_ATTEMPT_VIEW_KEYS },
    { name: "AuditEventView", keys: AUDIT_EVENT_VIEW_KEYS },
    { name: "DecisionResponse", keys: DECISION_RESPONSE_KEYS },
    { name: "DecisionRequest", keys: DECISION_REQUEST_KEYS },
    { name: "DeadLetterView", keys: DEAD_LETTER_VIEW_KEYS },
    { name: "RecoveryItemView", keys: RECOVERY_ITEM_VIEW_KEYS },
    { name: "ResolveRequest", keys: RESOLVE_REQUEST_KEYS },
  ];

  for (const model of models) {
    it(`${model.name} declares exactly the fields the console reads`, () => {
      const properties = Object.keys(schema(model.name).properties ?? {}).sort();
      expect(properties).toEqual([...model.keys].sort());
    });
  }

  it("every required field of ExceptionSummary is one the console renders", () => {
    // A required field the console ignores is a fact hidden from a reviewer, which is the failure
    // mode this whole screen exists to avoid.
    for (const field of schema("ExceptionSummary").required ?? []) {
      expect(EXCEPTION_SUMMARY_KEYS).toContain(field);
    }
  });

  it("a decision request may carry no principal", () => {
    // The actor is resolved from the bearer token upstream. If a `principal` field ever appears
    // here, the console must not start sending one — this test is the reminder.
    expect(Object.keys(schema("DecisionRequest").properties ?? {})).not.toContain("principal");
  });
});

describe("closed vocabularies match the committed contract", () => {
  it("TreatmentCode is the four codes the console knows", () => {
    expect(schema("TreatmentCode").enum).toEqual([...TREATMENT_CODES]);
  });

  it("no treatment code encodes a number", () => {
    // A treatment carrying a digit — `write_off_125_50` — would be the numeric escape hatch the
    // containment argument exists to prevent, and the console would render it as a label.
    for (const code of schema("TreatmentCode").enum ?? []) {
      expect(code).not.toMatch(/\d/);
    }
  });

  it("RecoveryResolution is the three the console offers", () => {
    expect(schema("RecoveryResolution").enum).toEqual([...RECOVERY_RESOLUTIONS]);
  });

  it("confidence is never a number anywhere in the contract", () => {
    // The bands live on a database column rather than in this document, so the assertion available
    // here is the one that matters: nothing in the schema offers a numeric confidence, so there is
    // no percentage for the console to have rendered.
    expect(CONFIDENCE_BANDS).toEqual(["low", "medium", "high"]);

    const numericConfidence = JSON.stringify(document.components.schemas).match(
      /"confidence"\s*:\s*\{[^}]*"type"\s*:\s*"(?:number|integer)"/,
    );
    expect(numericConfidence).toBeNull();
  });
});

describe("the endpoints the console calls exist in the contract", () => {
  const required = [
    "/api/v1/exceptions",
    "/api/v1/exceptions/{exception_id}",
    "/api/v1/exceptions/{exception_id}/approve",
    "/api/v1/exceptions/{exception_id}/edit",
    "/api/v1/exceptions/{exception_id}/reject",
    "/api/v1/dlq",
    "/api/v1/recovery",
    "/api/v1/recovery/{recovery_id}/resolve",
    "/healthz",
  ];

  for (const path of required) {
    it(`publishes ${path}`, () => {
      expect(Object.keys(document.paths)).toContain(path);
    });
  }
});

describe("capability detection", () => {
  /**
   * The console asks the control plane which optional endpoints exist rather than hard-coding the
   * answer, so a disabled control enables itself when the route is built.
   *
   * **The snapshot has moved once, and this is what that looked like.** When it was first taken
   * none of the five existed. Four have since shipped — replay, the demo fault control, identity
   * and instance metadata — and `request_edit` has not. Flipping these four was the moment to
   * check that each control's disabled copy had been removed, which is exactly why the expectation
   * is written out rather than derived from the document it is checking.
   */
  it("reports what the committed snapshot actually publishes", () => {
    expect(capabilitiesFromDocument(document)).toEqual({
      dlq_replay: true,
      demo_inject_crash: true,
      identity: true,
      meta: true,
      request_edit: false,
    });
  });

  it("detects each capability when the path is present", () => {
    const detected = capabilitiesFromDocument({
      paths: {
        "/api/v1/dlq/{dlq_id}/replay": {},
        "/api/v1/demo/exceptions/{exception_id}/inject-fault": {},
        "/api/v1/me": {},
        "/api/v1/meta": {},
        "/api/v1/exceptions/{exception_id}/request-edit": {},
      },
    });
    expect(detected).toEqual({
      dlq_replay: true,
      demo_inject_crash: true,
      identity: true,
      meta: true,
      request_edit: true,
    });
  });

  it("does not mistake the old contract for the shipped one", () => {
    // The paths this console was first written against. Keeping them as a negative case pins the
    // reconciliation: if someone reverts the probe to the guessed spelling, the snapshot test above
    // goes red rather than the console silently disabling two working controls.
    const superseded = capabilitiesFromDocument({
      paths: {
        "/api/v1/dlq/{dead_letter_id}/replay": {},
        "/api/v1/demo/inject-crash": {},
      },
    });
    expect(superseded.dlq_replay).toBe(false);
    expect(superseded.demo_inject_crash).toBe(false);
  });

  it("fails closed on an unreachable or unparseable document", () => {
    expect(capabilitiesFromDocument(null).dlq_replay).toBe(false);
    expect(capabilitiesFromDocument({ paths: "not an object" }).identity).toBe(false);
  });
});
