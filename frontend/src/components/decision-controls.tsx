"use client";

/**
 * The human gate: the only place in this console where a person authorises anything.
 *
 * Three verbs, and they are not interchangeable. **Approve** authorises the treatment the model
 * proposed. **Authorise a different treatment** is a separate call with a stricter rule — controller
 * only, and never the principal who requested the change. **Reject** authorises nothing and must
 * not name a treatment at all.
 *
 * What is authorised is a *treatment code*. No amount is chosen here, entered here, or shown here as
 * editable: the deterministic calculator turns the authorised code into money afterwards, and the
 * result appears in the adjustment panel. That ordering is the design, and a console that let an
 * operator type an amount into the approval form would have thrown it away.
 *
 * Every submission claims an idempotency key. It is generated at the moment of the click, sent
 * unchanged, and returned by the API — so a double-submitted form records one decision, and the
 * second attempt is refused as a consumed token rather than accepted as a second authorisation.
 */

import { useState } from "react";

import { useConsole } from "@/components/console-session";
import { Badge, Field, Panel, Timestamp } from "@/components/primitives";
import { Failure } from "@/components/states";
import { claimApprovalToken, submitDecision } from "@/lib/client";
import { mayApprove, mayEditTreatment, mayReject, mayRequestEdit } from "@/lib/roles";
import {
  TREATMENT_CODES,
  type ApiFailure,
  type DecisionResponse,
  type DecisionVerb,
  type ExceptionDetail,
  type TreatmentCode,
} from "@/lib/types";

/**
 * The version this decision addresses.
 *
 * An undecided exception is at version 1. A *superseding* decision needs the next version, and this
 * console cannot offer one: `GET /api/v1/exceptions/{id}` returns the approval without its
 * `resolution_version`, so there is no number to increment. Rather than guess — a wrong version
 * addresses a different resolution — the console shows the recorded decision and stops. The missing
 * field is listed in `frontend/README.md`.
 */
const FIRST_RESOLUTION_VERSION = 1;

export function DecisionControls({
  detail,
  onDecided,
}: {
  detail: ExceptionDetail;
  onDecided: () => void;
}) {
  const { session } = useConsole();
  const role = session.role;
  const unverified = role === null;

  const [pending, setPending] = useState<DecisionVerb | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [outcome, setOutcome] = useState<DecisionResponse | null>(null);
  const [chosenTreatment, setChosenTreatment] = useState<TreatmentCode>("escalate");
  const [requestedBy, setRequestedBy] = useState("");

  const proposal = detail.proposal;
  const proposedTreatment =
    proposal !== null && typeof proposal.treatment === "string"
      ? (proposal.treatment as TreatmentCode)
      : null;

  const canApprove = unverified || mayApprove(role);
  const canEdit = unverified || mayEditTreatment(role);
  const canReject = unverified || mayReject(role);
  const canRequestEdit = !unverified && mayRequestEdit(role);

  async function decide(verb: DecisionVerb, treatment: TreatmentCode | null, requester?: string) {
    setPending(verb);
    setFailure(null);
    setOutcome(null);
    const result = await submitDecision(detail.id, {
      verb,
      resolution_version: FIRST_RESOLUTION_VERSION,
      approval_token: claimApprovalToken(),
      ...(treatment === null ? {} : { treatment }),
      ...(proposal?.id ? { treatment_proposal_id: proposal.id } : {}),
      ...(requester ? { requested_by: requester } : {}),
    });
    setPending(null);
    if (result.ok) {
      setOutcome(result.data);
      onDecided();
    } else {
      setFailure(result.failure);
    }
  }

  if (detail.approval !== null) {
    return (
      <Panel
        title="Human decision"
        subtitle="Recorded. A superseding decision needs a new resolution version and is not offered here."
        provenance={{ label: "human authority", tone: "operator" }}
      >
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-5">
          <Field label="Decision">
            <Badge tone={detail.approval.decision === "rejected" ? "refusal" : "operator"}>
              {detail.approval.decision ?? "not recorded"}
            </Badge>
          </Field>
          <Field label="Authorised treatment">
            {detail.approval.approved_treatment ? (
              <Badge tone="deterministic">{detail.approval.approved_treatment}</Badge>
            ) : null}
          </Field>
          <Field label="Authorised by" mono>
            {detail.approval.principal}
          </Field>
          <Field label="Requested by" mono>
            {detail.approval.requested_by}
          </Field>
          <Field label="Decided at">
            <Timestamp value={detail.approval.decided_at} />
          </Field>
        </dl>
        <p className="mt-3 text-ink-faint">
          A rejection authorises nothing, so an exception with a rejected decision has no adjustment
          and never will at this resolution version.
        </p>
        {/* Rendered here too, because the decision just taken lands in this branch on the reload
            that follows it — and the returned idempotency key is the thing worth showing. */}
        {outcome !== null ? <DecisionOutcome outcome={outcome} /> : null}
        {failure !== null ? (
          <div className="mt-3">
            <Failure failure={failure} />
          </div>
        ) : null}
      </Panel>
    );
  }

  return (
    <Panel
      title="Human decision"
      subtitle="Authorising a treatment code — never an amount. The calculator prices it afterwards."
      provenance={{ label: "human authority", tone: "operator" }}
    >
      <div className="space-y-4">
        <p className="text-ink-dim">
          Authority is decided by the control plane from your bearer token, never from anything this
          page sends. The controls below are the ones your role is documented to hold; a refusal is
          reported as an authority message, not hidden.
        </p>

        {!canApprove && !canReject ? (
          <div className="rounded border border-edge bg-panel-raised px-3 py-2.5 text-ink-dim">
            Your role holds no approval right. The operator role works the dead-letter and recovery
            queues; giving it authority over the amount it is unsticking would collapse the
            separation the design rests on.
          </div>
        ) : null}

        {canApprove ? (
          <div className="rounded border border-edge bg-panel-raised px-3 py-3">
            <h3 className="font-medium text-ink">Approve</h3>
            {proposedTreatment !== null ? (
              <>
                <p className="mt-1 text-ink-dim">
                  Authorises the proposed treatment{" "}
                  <Badge tone="model">{proposedTreatment}</Badge> unchanged. To authorise anything
                  else, use the countersigned path below.
                </p>
                <button
                  type="button"
                  disabled={pending !== null}
                  onClick={() => void decide("approve", proposedTreatment)}
                  className="mt-3 rounded bg-operator px-3 py-1.5 font-medium text-surface disabled:opacity-40"
                >
                  {pending === "approve" ? "Recording…" : `Approve ${proposedTreatment}`}
                </button>
              </>
            ) : (
              <>
                <p className="mt-1 text-ink-dim">
                  There is no proposal to approve, so there is nothing to differ from: name the
                  treatment you are authorising. It is yours, not the model&apos;s.
                </p>
                <div className="mt-3 flex flex-wrap items-end gap-3">
                  <label className="flex flex-col gap-1">
                    <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                      Treatment
                    </span>
                    <select
                      aria-label="Treatment"
                      value={chosenTreatment}
                      onChange={(event) => setChosenTreatment(event.target.value as TreatmentCode)}
                      className="rounded border border-edge bg-surface px-2 py-1.5 text-ink"
                    >
                      {TREATMENT_CODES.map((code) => (
                        <option key={code} value={code}>
                          {code}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={pending !== null}
                    onClick={() => void decide("approve", chosenTreatment)}
                    className="rounded bg-operator px-3 py-1.5 font-medium text-surface disabled:opacity-40"
                  >
                    {pending === "approve" ? "Recording…" : "Authorise treatment"}
                  </button>
                </div>
              </>
            )}
          </div>
        ) : null}

        {canEdit ? (
          <div className="rounded border border-edge bg-panel-raised px-3 py-3">
            <h3 className="font-medium text-ink">Authorise a different treatment</h3>
            <p className="mt-1 text-ink-dim">
              A separate authorisation with a stricter rule: controller only, and the authoriser may
              not be the principal who requested the change. Name that principal — the control plane
              refuses the decision if it is you.
            </p>
            <div className="mt-3 flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                  Treatment
                </span>
                <select
                  aria-label="Edited treatment"
                  value={chosenTreatment}
                  onChange={(event) => setChosenTreatment(event.target.value as TreatmentCode)}
                  className="rounded border border-edge bg-surface px-2 py-1.5 text-ink"
                >
                  {TREATMENT_CODES.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                  Requested by
                </span>
                <input
                  aria-label="Requested by"
                  value={requestedBy}
                  onChange={(event) => setRequestedBy(event.target.value)}
                  placeholder="principal who asked"
                  className="tabular rounded border border-edge bg-surface px-2 py-1.5 text-ink placeholder:text-ink-faint"
                />
              </label>
              <button
                type="button"
                disabled={pending !== null || requestedBy.trim().length === 0}
                onClick={() => void decide("edit", chosenTreatment, requestedBy.trim())}
                className="rounded border border-operator px-3 py-1.5 font-medium text-operator disabled:opacity-40"
              >
                {pending === "edit" ? "Recording…" : "Authorise edited treatment"}
              </button>
            </div>
          </div>
        ) : null}

        {canRequestEdit ? (
          <div className="rounded border border-dashed border-edge bg-panel-raised px-3 py-3">
            <h3 className="font-medium text-ink">Request a different treatment</h3>
            <p className="mt-1 text-ink-dim">
              An analyst may ask for a different treatment without authorising one. There is no
              endpoint for that request on this control plane: the requesting principal is carried
              only as a field on a controller&apos;s authorisation, so the console cannot file it.
            </p>
            <button
              type="button"
              disabled
              title="POST /api/v1/exceptions/{id}/request-edit does not exist on this control plane."
              className="mt-3 cursor-not-allowed rounded border border-edge px-3 py-1.5 text-ink-faint"
            >
              Request edit — endpoint not implemented
            </button>
          </div>
        ) : null}

        {canReject ? (
          <div className="rounded border border-edge bg-panel-raised px-3 py-3">
            <h3 className="font-medium text-ink">Reject</h3>
            <p className="mt-1 text-ink-dim">
              Declines the proposal. It authorises nothing and must not name a treatment, so no
              amount is ever computed for a rejected exception.
            </p>
            <button
              type="button"
              disabled={pending !== null}
              onClick={() => void decide("reject", null)}
              className="mt-3 rounded border border-refusal px-3 py-1.5 font-medium text-refusal disabled:opacity-40"
            >
              {pending === "reject" ? "Recording…" : "Reject"}
            </button>
          </div>
        ) : null}

        {outcome !== null ? <DecisionOutcome outcome={outcome} /> : null}

        {failure !== null ? <Failure failure={failure} /> : null}
      </div>
    </Panel>
  );
}

/** What the control plane recorded, including the idempotency key it consumed. */
function DecisionOutcome({ outcome }: { outcome: DecisionResponse }) {
  return (
    <div
      role="status"
      className="mt-3 rounded border border-deterministic/40 bg-deterministic-bg px-3 py-2.5"
    >
      <p className="font-semibold text-deterministic">Decision recorded</p>
      <dl className="mt-2 grid gap-x-6 gap-y-2 sm:grid-cols-4">
        <Field label="Decision">{outcome.decision}</Field>
        <Field label="Authorised treatment">{outcome.approved_treatment}</Field>
        <Field label="Principal" mono>
          {outcome.principal}
        </Field>
        <Field label="Idempotency key returned" mono>
          {outcome.approval_token}
        </Field>
      </dl>
      <p className="mt-2 text-ink-faint">
        The key is the one this console claimed and the control plane consumed. Submitting it again
        is refused, which is what makes a double-clicked form one decision.
      </p>
    </div>
  );
}
