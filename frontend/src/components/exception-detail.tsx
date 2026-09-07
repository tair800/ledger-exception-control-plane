"use client";

/**
 * Full provenance for one exception, in the order a reviewer follows it: what happened, what
 * evidence was assembled, what the model proposed from it, who authorised what, what was computed,
 * what was dispatched, and what the append-only trail recorded.
 *
 * The screen is built around one read — `GET /api/v1/exceptions/{id}` returns all of it — because
 * 7.1's exit criterion is that provenance for any exception is reachable in two clicks, and a
 * reviewer following a posting backwards should not have to assemble it from six requests.
 *
 * **The panels are colour-coded by who produced their contents.** Amber is the model, green is
 * deterministic code, blue is an operator action. That is not decoration: the single most important
 * thing a reader should take away is which parts of a financial decision a language model touched,
 * and it touched exactly one panel.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { DecisionControls } from "@/components/decision-controls";
import { Badge, Field, Money, Panel, Timestamp } from "@/components/primitives";
import { Empty, Failure, Loading } from "@/components/states";
import { listRecovery, readException } from "@/lib/client";
import type {
  ApiFailure,
  ConfidenceBand,
  EvidenceView,
  ExceptionDetail as Detail,
  RecoveryItemView,
} from "@/lib/types";
import { CONFIDENCE_BANDS } from "@/lib/types";

export function ExceptionDetailView({ exceptionId }: { exceptionId: string }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  /**
   * `refresh` re-reads without blanking the screen.
   *
   * That matters after a decision: clearing `detail` would unmount the decision panel and take the
   * confirmation — including the idempotency key the control plane consumed — with it, exactly at
   * the moment the operator needs to see what was recorded.
   */
  const load = useCallback(
    async (mode: "initial" | "refresh" = "initial") => {
      if (mode === "initial") setDetail(null);
      setFailure(null);
      const result = await readException(exceptionId);
      if (result.ok) setDetail(result.data);
      else setFailure(result.failure);
    },
    [exceptionId],
  );

  useEffect(() => {
    void load("initial");
  }, [load]);

  if (failure !== null) return <Failure failure={failure} onRetry={() => void load("initial")} />;
  if (detail === null) return <Loading label="Loading provenance for this exception…" />;

  return (
    <div className="space-y-4">
      <ExceptionHeader detail={detail} />

      <div className="grid gap-4 lg:grid-cols-2">
        <EvidencePanel evidence={detail.evidence} />
        <ProposalPanel detail={detail} />
      </div>

      <DecisionControls detail={detail} onDecided={() => void load("refresh")} />

      <div className="grid gap-4 lg:grid-cols-2">
        <AdjustmentPanel detail={detail} />
        <DispatchPanel detail={detail} />
      </div>

      <RecoveryPanel detail={detail} />
      <AuditPanel detail={detail} />
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// 1 — the exception and the line it was raised on
// ---------------------------------------------------------------------------------------------

function ExceptionHeader({ detail }: { detail: Detail }) {
  return (
    <Panel
      title="Exception"
      subtitle="A settlement residual the deterministic matcher could not clear."
      provenance={{ label: "deterministic", tone: "deterministic" }}
    >
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-3 lg:grid-cols-4">
        <Field label="Classification">{detail.classification}</Field>
        <Field label="Status">
          <Badge tone={detail.status === "resolved" ? "deterministic" : "neutral"}>
            {detail.status}
          </Badge>
        </Field>
        <Field label="Exception id" mono>
          {detail.id}
        </Field>
        <Field label="Correlation id" mono>
          {detail.correlation_id}
        </Field>
        <Field label="PSP reference" mono>
          {detail.line.psp_reference}
        </Field>
        <Field label="Merchant reference" mono>
          {detail.line.merchant_reference}
        </Field>
        <Field label="Transaction type">{detail.line.transaction_type}</Field>
        <Field label="Value date" mono>
          {detail.line.value_date}
        </Field>
        <Field label="Settlement line amount">
          <Money amount={detail.line.amount} currency={detail.line.currency} />
        </Field>
      </dl>
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 2 — the evidence pack, and which of it was cited
// ---------------------------------------------------------------------------------------------

function EvidencePanel({ evidence }: { evidence: EvidenceView[] }) {
  const cited = evidence.filter((item) => item.cited).length;

  return (
    <Panel
      title="Evidence pack"
      subtitle={
        evidence.length === 0
          ? "Assembled deterministically from settlement, ledger and support records."
          : `${evidence.length} records assembled; ${cited} cited by the proposal.`
      }
      provenance={{ label: "deterministic", tone: "deterministic" }}
    >
      {evidence.length === 0 ? (
        <Empty
          title="No evidence assembled"
          detail="Nothing was gathered for this exception, so no proposal could have been grounded in anything."
        />
      ) : (
        <ul className="space-y-2">
          {evidence.map((item) => (
            <li
              key={item.id}
              className={`rounded border px-3 py-2 ${
                item.cited ? "border-model/40 bg-model-bg/40" : "border-edge bg-panel-raised"
              }`}
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{item.kind}</span>
                {item.cited ? (
                  <Badge tone="model">cited by the proposal</Badge>
                ) : (
                  <Badge tone="neutral">not cited</Badge>
                )}
                <span className="tabular ml-auto text-ink-faint">{item.id}</span>
              </div>
              <p className="mt-1 whitespace-pre-wrap text-ink-dim">{item.content}</p>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 3, 4, 5, 6 — the proposal: treatment, confidence band, abstention, evidence references
// ---------------------------------------------------------------------------------------------

/**
 * Confidence as one of three bands.
 *
 * There is no percentage here and no proportional bar, because the model does not emit a number:
 * its response schema contains no numeric type anywhere in its tree. A bar filled to two-thirds for
 * "medium" would be a number this console invented, and a reviewer would reasonably read it as one
 * the model produced.
 */
function ConfidenceBandView({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-ink-faint">not recorded</span>;

  const known = (CONFIDENCE_BANDS as readonly string[]).includes(value)
    ? (value as ConfidenceBand)
    : null;

  return (
    <span className="flex flex-wrap items-center gap-2">
      <Badge tone="model">{value}</Badge>
      {known === null ? (
        <span className="text-refusal">band not recognised by this console</span>
      ) : (
        <span className="text-ink-faint">
          one of {CONFIDENCE_BANDS.join(" / ")} — a band, never a number
        </span>
      )}
    </span>
  );
}

function ProposalPanel({ detail }: { detail: Detail }) {
  const proposal = detail.proposal;
  const citedEvidence = useMemo(() => detail.evidence.filter((item) => item.cited), [detail.evidence]);

  if (proposal === null) {
    return (
      <Panel
        title="Treatment proposal"
        subtitle="What the model proposed, if it was asked."
        provenance={{ label: "model output", tone: "model" }}
      >
        <Empty
          title="No proposal on record"
          detail="The model has not been asked about this exception, or was unavailable. A human may still decide: the proposal is advice, not a precondition."
        />
      </Panel>
    );
  }

  const abstained = proposal.abstained === true;

  return (
    <Panel
      title="Treatment proposal"
      subtitle="A categorical instruction and nothing else. The model selects what to do, never how much."
      provenance={{ label: "model output", tone: "model" }}
    >
      {abstained ? (
        <div className="mb-3 rounded border border-model/40 bg-model-bg px-3 py-2.5">
          <p className="font-semibold text-model">The model abstained</p>
          <p className="mt-1 text-ink-dim">
            It declined to propose a treatment for this exception. Abstention is a recorded outcome,
            not a missing answer, and it carries <code className="tabular">escalate</code> so the
            exception reaches a human rather than a default. Nothing was inferred on its behalf.
          </p>
        </div>
      ) : null}

      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
        <Field label="Proposed treatment">
          {proposal.treatment ? (
            <span className="flex items-center gap-2">
              <Badge tone="model">{proposal.treatment}</Badge>
              {abstained ? <span className="text-ink-faint">on abstention</span> : null}
            </span>
          ) : null}
        </Field>
        <Field label="Confidence">
          <ConfidenceBandView value={proposal.confidence} />
        </Field>
        <Field label="Model" mono>
          {proposal.model_id}
        </Field>
        <Field label="Model version" mono>
          {proposal.model_version}
        </Field>
        <Field label="Proposal id" mono>
          {proposal.id}
        </Field>
        <Field label="Evidence cited">
          {citedEvidence.length === 0 ? (
            <span className="text-ink-faint">none — the proposal cited no evidence record</span>
          ) : (
            <ul className="space-y-1">
              {citedEvidence.map((item) => (
                <li key={item.id} className="flex flex-wrap items-baseline gap-2">
                  <span className="text-ink">{item.kind}</span>
                  <span className="tabular text-ink-faint">{item.id}</span>
                </li>
              ))}
            </ul>
          )}
        </Field>
      </dl>

      <div className="mt-4 rounded border border-model/40 bg-model-bg px-3 py-2.5">
        <p className="text-[11px] font-medium uppercase tracking-wider text-model">
          Model-generated rationale — provenance for humans only
        </p>
        <p className="mt-1.5 whitespace-pre-wrap text-ink-dim">
          {proposal.rationale ?? "No rationale was recorded."}
        </p>
        <p className="mt-2 text-ink-faint">
          Free text, displayed unchanged and read by nobody but you. No code parses it, extracts a
          number from it, or branches on what it says. If it disagrees with the amount below, the
          amount below is the fact.
        </p>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 9, 10, 11 — the deterministic adjustment
// ---------------------------------------------------------------------------------------------

function AdjustmentPanel({ detail }: { detail: Detail }) {
  const adjustment = detail.adjustment;

  return (
    <Panel
      title="Deterministic adjustment"
      subtitle="Computed by typed Python from ledger and settlement data, after a human authorised the treatment."
      provenance={{ label: "deterministic", tone: "deterministic" }}
    >
      {adjustment === null ? (
        <Empty
          title="Nothing computed"
          detail="No amount exists until a treatment is authorised. Approving authorises a treatment code; the calculator turns that into money afterwards."
        />
      ) : (
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          <Field label="Amount" span>
            <Money amount={adjustment.amount} currency={adjustment.currency} size="large" />
            <p className="mt-1 text-ink-faint">
              Rendered exactly as the control plane returned it. The console does no arithmetic on
              monetary values: no totals, no conversion, no rounding, no reformatting.
            </p>
          </Field>
          <Field label="Account code" mono>
            {adjustment.account_code}
          </Field>
          <Field label="Period" mono>
            {adjustment.period}
          </Field>
          <Field label="Operation id" span mono>
            {adjustment.operation_id}
            <p className="mt-1 font-sans text-ink-faint">
              Retry-independent: derived from the instruction payload, with no attempt counter,
              timestamp, random value or approver identity in it. The same instruction re-sent
              carries the same identifier, which is what lets the ledger recognise a repeat.
            </p>
          </Field>
          <Field label="Posting reference" span mono>
            {adjustment.posting_ref}
          </Field>
        </dl>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 12, 13 — outbox state and posting attempts
// ---------------------------------------------------------------------------------------------

const OUTCOME_TONE: Record<string, "deterministic" | "refusal" | "model" | "neutral"> = {
  confirmed: "deterministic",
  rejected: "refusal",
  throttled: "model",
  unknown: "model",
  partially_applied: "refusal",
  not_sent: "neutral",
};

function DispatchPanel({ detail }: { detail: Detail }) {
  const outbox = detail.outbox;

  return (
    <Panel
      title="Dispatch"
      subtitle="The transactional outbox and every attempt it made. At-least-once by construction: it guarantees the intent is not lost, never that it was delivered once."
      provenance={{ label: "deterministic", tone: "deterministic" }}
    >
      {outbox === null ? (
        <Empty
          title="Nothing dispatched"
          detail="An outbox row is written in the same transaction as the adjustment, so there is nothing here until an amount exists."
        />
      ) : (
        <>
          <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-3">
            <Field label="Outbox state">
              <Badge
                tone={
                  outbox.state === "settled"
                    ? "deterministic"
                    : outbox.state === "dead_lettered"
                      ? "refusal"
                      : "neutral"
                }
              >
                {outbox.state ?? "not recorded"}
              </Badge>
            </Field>
            <Field label="Last outcome">
              {outbox.last_outcome ? (
                <Badge tone={OUTCOME_TONE[outbox.last_outcome] ?? "neutral"}>
                  {outbox.last_outcome}
                </Badge>
              ) : null}
            </Field>
            <Field label="Attempts recorded" mono>
              {outbox.attempt_count === null || outbox.attempt_count === undefined
                ? null
                : String(outbox.attempt_count)}
            </Field>
          </dl>

          {outbox.last_outcome === "unknown" ? (
            <p className="mt-3 rounded border border-model/40 bg-model-bg px-3 py-2 text-model">
              The outcome of the last attempt is ambiguous: the request was sent and no answer came
              back. It is not retried and not assumed failed — it goes to manual recovery, below.
            </p>
          ) : null}

          <h3 className="mt-4 mb-2 text-[11px] font-medium uppercase tracking-wider text-ink-faint">
            Posting attempts
          </h3>
          {detail.attempts.length === 0 ? (
            <Empty
              title="No attempt recorded"
              detail="A write-ahead attempt row is committed before every socket write, so an attempt with no outcome means a crash mid-send, not a missing record."
            />
          ) : (
            <div className="overflow-x-auto rounded border border-edge">
              <table className="w-full border-collapse text-left">
                <thead className="bg-panel-raised text-[11px] uppercase tracking-wider text-ink-faint">
                  <tr>
                    <th scope="col" className="px-3 py-1.5 font-medium">
                      #
                    </th>
                    <th scope="col" className="px-3 py-1.5 font-medium">
                      State
                    </th>
                    <th scope="col" className="px-3 py-1.5 font-medium">
                      Outcome
                    </th>
                    <th scope="col" className="px-3 py-1.5 font-medium">
                      Sent at
                    </th>
                    <th scope="col" className="px-3 py-1.5 font-medium">
                      Posting reference
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {detail.attempts.map((attempt) => (
                    <tr key={attempt.attempt_no} className="border-t border-edge">
                      <td className="tabular px-3 py-1.5">{attempt.attempt_no}</td>
                      <td className="px-3 py-1.5 text-ink-dim">{attempt.state}</td>
                      <td className="px-3 py-1.5">
                        {attempt.outcome ? (
                          <Badge tone={OUTCOME_TONE[attempt.outcome] ?? "neutral"}>
                            {attempt.outcome}
                          </Badge>
                        ) : (
                          <span className="text-ink-faint">in flight, no outcome recorded</span>
                        )}
                      </td>
                      <td className="px-3 py-1.5">
                        <Timestamp value={attempt.sent_at} />
                      </td>
                      <td className="tabular px-3 py-1.5 text-ink-dim">
                        {attempt.posting_ref ?? "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 14 — recovery status for this exception's adjustment
// ---------------------------------------------------------------------------------------------

/**
 * Whether this adjustment is open in manual recovery.
 *
 * `GET /api/v1/exceptions/{id}` carries no recovery field, so this matches the adjustment id
 * against the open recovery queue — a join on identifiers, which every role may read. The wording
 * is careful about what that can and cannot prove: an adjustment absent from the *open* queue was
 * either never in recovery or has been resolved, and this console cannot tell which. It says so
 * rather than reporting "no recovery needed", which would be a claim it has no basis for.
 */
function RecoveryPanel({ detail }: { detail: Detail }) {
  const adjustmentId = detail.adjustment?.id ?? null;
  const [items, setItems] = useState<RecoveryItemView[] | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  useEffect(() => {
    if (adjustmentId === null) return;
    let live = true;
    void listRecovery(false).then((result) => {
      if (!live) return;
      if (result.ok) setItems(result.data);
      else setFailure(result.failure);
    });
    return () => {
      live = false;
    };
  }, [adjustmentId]);

  if (adjustmentId === null) return null;

  const match = items?.find((item) => item.adjustment_id === adjustmentId) ?? null;

  return (
    <Panel
      title="Manual recovery"
      subtitle="Where the automatic path stops. An ambiguous outcome is never retried and never assumed."
      provenance={{ label: "operator queue", tone: "operator" }}
    >
      {failure !== null ? (
        <Failure failure={failure} />
      ) : items === null ? (
        <Loading label="Checking the recovery queue…" />
      ) : match === null ? (
        <p className="text-ink-dim">
          This adjustment is not open in the recovery queue. It either never entered recovery or has
          already been resolved — the open queue cannot distinguish the two, and this console will
          not guess.
        </p>
      ) : (
        <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2">
          <Field label="Recovery item" mono>
            {match.id}
          </Field>
          <Field label="Reason">
            <span className="flex items-center gap-2">
              <Badge tone="operator">{match.reason}</Badge>
              {match.overdue ? <Badge tone="refusal">past SLA</Badge> : null}
            </span>
          </Field>
          <Field label="Opened at">
            <Timestamp value={match.opened_at} />
          </Field>
          <Field label="SLA due at">
            <Timestamp value={match.sla_due_at} />
          </Field>
          <Field label="Evidence procedure" span>
            <span className="whitespace-pre-wrap">{match.evidence_procedure}</span>
          </Field>
        </dl>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// 15 — the append-only trail
// ---------------------------------------------------------------------------------------------

function AuditPanel({ detail }: { detail: Detail }) {
  return (
    <Panel
      title="Audit trail"
      subtitle={`Append-only, keyed on the correlation id so it spans ingestion through posting. ${detail.audit.length} events.`}
      provenance={{ label: "append-only", tone: "deterministic" }}
    >
      {detail.audit.length === 0 ? (
        <Empty
          title="No audit events"
          detail="Nothing has been recorded against this correlation id. Every decision the system takes emits one, so an empty trail means nothing has happened yet."
        />
      ) : (
        <ol className="space-y-2">
          {detail.audit.map((event) => (
            <li key={event.id} className="rounded border border-edge bg-panel-raised px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={event.outcome === "failure" ? "refusal" : "neutral"}>
                  {event.tool}
                </Badge>
                <span className="text-ink-dim">{event.outcome}</span>
                <span className="tabular ml-auto text-ink-faint">{event.occurred_at}</span>
              </div>
              <dl className="mt-2 grid gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
                <Field label="Principal" mono>
                  {event.principal}
                </Field>
                <Field label="Scope granted" mono>
                  {event.scope_granted}
                </Field>
                <Field label="Decision">{event.approval_decision}</Field>
                <Field label="Approver" mono>
                  {event.approver}
                </Field>
                <Field label="Model" mono>
                  {event.model}
                </Field>
              </dl>
              {event.agent_identity === null || event.region_jurisdiction === null ? (
                <p className="mt-2 text-ink-faint">
                  {event.agent_identity === null ? "No agent identity: this system is not an agent. " : ""}
                  {event.region_jurisdiction === null
                    ? "No processing region: no model call was made on this event."
                    : ""}
                  {" "}
                  Named rather than left blank — a trail that renders an absent field as empty space
                  looks like a trail with nothing to say.
                </p>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
