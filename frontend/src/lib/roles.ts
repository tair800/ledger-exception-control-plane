/**
 * Which controls a role may see. A **rendering** rule, never an authorisation.
 *
 * Authorisation is server-side and fails closed: `routes.py` resolves the role from the bearer
 * token, never from anything the client says, and refuses before it reads. This module exists so
 * the console does not present a button whose only outcome is a 403 — a control that always refuses
 * teaches an operator to distrust the interface.
 *
 * The predicates below mirror `security.py` deliberately, and `src/test/roles.test.ts` pins each
 * one against the rule stated there. If the backend ever widens a role, this file is wrong until
 * it is changed, and the test says so.
 */

import type { Role } from "@/lib/types";

/**
 * Only a controller authorises a proposed treatment. The operator holds no approval right at all.
 *
 * **This rule was once stricter than the server's, and that turned out to be a real defect rather
 * than a difference of opinion.** `security.py::APPROVAL_ROLES` held both `ANALYST` and
 * `CONTROLLER` and `record_decision` checked it once, so an analyst token was accepted at
 * `POST /exceptions/{id}/approve` — contradicting ADR-056 §2, which states in a table that an
 * analyst may not approve. This console rendered the documented rule and reported the mismatch
 * instead of following the code, which is how the hole was found.
 *
 * The server now separates `may_record_decision` from `may_authorise` and refuses the analyst the
 * second. `GET /api/v1/me` returns both, so a future version of this file can read the authority
 * the server will actually enforce rather than restating it — which is the better arrangement, and
 * the reason the endpoint returns capabilities and not just a role name.
 */
export function mayApprove(role: Role): boolean {
  return role === "controller";
}

/**
 * Only a controller may authorise a treatment different from the one proposed, and §16's
 * countersignature rule applies on top: the controller must differ from the requester. That second
 * half is enforced server-side only — the console does not know who requested until it asks.
 */
export function mayEditTreatment(role: Role): boolean {
  return role === "controller";
}

/** Rejecting authorises no money to move, so an analyst may do it. */
export function mayReject(role: Role): boolean {
  return role === "analyst" || role === "controller";
}

/**
 * An analyst may *request* a different treatment without authorising one.
 *
 * There is no endpoint for that request today: `requested_by` exists only as a field on the
 * controller's `/edit` call, and a decision that is not `EDITED` is refused for carrying it. So the
 * console renders the control disabled and says why, rather than pretending the request was filed.
 */
export function mayRequestEdit(role: Role): boolean {
  return role === "analyst";
}

/** The dead-letter and recovery queues are operator work. Deliberately not an approval role. */
export function mayWorkOperationsQueues(role: Role): boolean {
  return role === "operator";
}

/** Every configured role reads the queues; only some may act on them. */
export function mayReadExceptions(): boolean {
  return true;
}

/** One-line description of what a role is for, shown beside the signed-in principal. */
export const ROLE_SUMMARY: Record<Role, string> = {
  analyst: "Reads the queue, may reject, may request a different treatment.",
  controller: "May approve, and is the only role that may authorise an edited treatment.",
  operator: "Works the dead-letter and recovery queues. Holds no approval right.",
};
