import { describe, expect, it } from "vitest";

import {
  mayApprove,
  mayEditTreatment,
  mayReject,
  mayRequestEdit,
  mayWorkOperationsQueues,
} from "@/lib/roles";
import { ROLES } from "@/lib/types";

/**
 * Role separation, as the console renders it.
 *
 * These predicates decide which *controls* appear. They are not authorisation — the control plane
 * resolves the role from the bearer token and refuses before it reads anything, and nothing this
 * file says can widen that. The point of pinning them is that a console which offers a button whose
 * only outcome is a refusal teaches an operator to distrust the interface.
 */
describe("which controls each role is shown", () => {
  it("only a controller is offered approval", () => {
    expect(mayApprove("controller")).toBe(true);
    expect(mayApprove("analyst")).toBe(false);
    expect(mayApprove("operator")).toBe(false);
  });

  it("only a controller is offered an edited treatment", () => {
    expect(mayEditTreatment("controller")).toBe(true);
    expect(mayEditTreatment("analyst")).toBe(false);
    expect(mayEditTreatment("operator")).toBe(false);
  });

  it("an analyst may reject, because a rejection authorises no money to move", () => {
    expect(mayReject("analyst")).toBe(true);
    expect(mayReject("controller")).toBe(true);
    expect(mayReject("operator")).toBe(false);
  });

  it("only an analyst is shown the request-an-edit path", () => {
    expect(mayRequestEdit("analyst")).toBe(true);
    expect(mayRequestEdit("controller")).toBe(false);
    expect(mayRequestEdit("operator")).toBe(false);
  });

  it("only an operator works the failure queues, and holds no approval right", () => {
    expect(mayWorkOperationsQueues("operator")).toBe(true);
    expect(mayWorkOperationsQueues("analyst")).toBe(false);
    expect(mayWorkOperationsQueues("controller")).toBe(false);

    // The separation the design rests on: the role that unsticks a dispatch may not authorise the
    // amount it is unsticking.
    expect(mayApprove("operator")).toBe(false);
    expect(mayEditTreatment("operator")).toBe(false);
  });

  it("no role holds every authority", () => {
    for (const role of ROLES) {
      const held = [
        mayApprove(role),
        mayEditTreatment(role),
        mayReject(role),
        mayWorkOperationsQueues(role),
      ];
      expect(held.every(Boolean), `${role} holds everything`).toBe(false);
    }
  });

  it("the three roles are exactly the ones the control plane defines", () => {
    expect([...ROLES]).toEqual(["analyst", "controller", "operator"]);
  });
});
