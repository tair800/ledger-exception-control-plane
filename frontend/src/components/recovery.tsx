"use client";

/**
 * The manual-recovery queue: operations whose outcome is ambiguous.
 *
 * An ambiguous outcome is a first-class state, not an error. The request was sent, no answer came
 * back, and the system will not retry an irreversible financial write on the assumption it failed —
 * that is the exact defect this project exists to prevent. So it stops here, and an operator
 * inspects the ledger.
 *
 * **Recording a finding never causes a posting.** There is no re-send control on this screen and
 * there is no endpoint behind one. `resolved_unverified` exists so a judgement made without
 * obtainable evidence stays visible to an auditor rather than looking like a verified one.
 */

import { useCallback, useEffect, useState } from "react";

import { Badge, Timestamp } from "@/components/primitives";
import { Empty, Failure, Loading } from "@/components/states";
import { listRecovery, resolveRecovery } from "@/lib/client";
import { RECOVERY_RESOLUTIONS, type ApiFailure, type RecoveryItemView, type RecoveryResolution } from "@/lib/types";

const RESOLUTION_LABEL: Record<RecoveryResolution, string> = {
  confirmed_by_evidence: "Confirmed by evidence — the ledger holds the posting",
  rejected_by_evidence: "Rejected by evidence — the ledger does not hold it",
  resolved_unverified: "Resolved unverified — no obtainable evidence either way",
};

export function RecoveryQueue() {
  const [rows, setRows] = useState<RecoveryItemView[] | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [staleOnly, setStaleOnly] = useState(false);

  const load = useCallback(async () => {
    setRows(null);
    setFailure(null);
    const result = await listRecovery(staleOnly);
    if (result.ok) setRows(result.data);
    else setFailure(result.failure);
  }, [staleOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Manual recovery</h1>
          <p className="max-w-prose text-ink-dim">
            Where the automatic path stops. Each item names what to inspect and what would be
            sufficient, because a queue that returns a reason code and expects the operator to look
            the procedure up elsewhere is not a control.
          </p>
        </div>
        <label className="flex items-center gap-2 text-ink-dim">
          <input
            type="checkbox"
            checked={staleOnly}
            onChange={(event) => setStaleOnly(event.target.checked)}
            className="h-4 w-4"
          />
          Past SLA only
        </label>
      </div>

      {failure !== null ? (
        <Failure failure={failure} onRetry={() => void load()} />
      ) : rows === null ? (
        <Loading label="Loading the recovery queue…" />
      ) : rows.length === 0 ? (
        <Empty
          title={staleOnly ? "Nothing past its SLA" : "No open recovery items"}
          detail={
            staleOnly
              ? "Every open item is still inside its service-level window. A stale ambiguous outcome is an alertable condition, and there are none."
              : "No dispatch on this control plane is waiting on an operator's judgement."
          }
        />
      ) : (
        <ul className="space-y-3">
          {rows.map((item) => (
            <RecoveryItem key={item.id} item={item} onResolved={() => void load()} />
          ))}
        </ul>
      )}
    </div>
  );
}

function RecoveryItem({ item, onResolved }: { item: RecoveryItemView; onResolved: () => void }) {
  const [resolution, setResolution] = useState<RecoveryResolution>("confirmed_by_evidence");
  const [postingRef, setPostingRef] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  const needsReference = resolution === "confirmed_by_evidence";

  async function submit() {
    setPending(true);
    setFailure(null);
    const result = await resolveRecovery(
      item.id,
      resolution,
      needsReference ? postingRef.trim() : undefined,
    );
    setPending(false);
    if (result.ok) onResolved();
    else setFailure(result.failure);
  }

  return (
    <li className="rounded-md border border-edge bg-panel">
      <div className="flex flex-wrap items-center gap-2 border-b border-edge px-4 py-2.5">
        <span className="tabular text-ink">{item.operation_id}</span>
        <Badge tone="operator">{item.reason}</Badge>
        {item.overdue ? <Badge tone="refusal">past SLA</Badge> : null}
        <span className="ml-auto text-ink-faint">
          opened <Timestamp value={item.opened_at} /> · due <Timestamp value={item.sla_due_at} />
        </span>
      </div>

      <div className="space-y-3 px-4 py-3">
        <div>
          <p className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
            Evidence procedure
          </p>
          <p className="mt-1 whitespace-pre-wrap text-ink-dim">{item.evidence_procedure}</p>
        </div>

        <p className="text-ink-faint">
          Authorised by <span className="tabular">{item.approving_principal}</span> — who may not
          also judge what happened to it. Adjustment{" "}
          <span className="tabular">{item.adjustment_id}</span>.
        </p>

        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
              What you found
            </span>
            <select
              aria-label="Resolution"
              value={resolution}
              onChange={(event) => setResolution(event.target.value as RecoveryResolution)}
              className="rounded border border-edge bg-surface px-2 py-1.5 text-ink"
            >
              {RECOVERY_RESOLUTIONS.map((value) => (
                <option key={value} value={value}>
                  {RESOLUTION_LABEL[value]}
                </option>
              ))}
            </select>
          </label>

          {needsReference ? (
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                Posting reference
              </span>
              <input
                aria-label="Posting reference"
                value={postingRef}
                onChange={(event) => setPostingRef(event.target.value)}
                placeholder="the reference that evidences it"
                className="tabular rounded border border-edge bg-surface px-2 py-1.5 text-ink placeholder:text-ink-faint"
              />
            </label>
          ) : null}

          <button
            type="button"
            disabled={pending || (needsReference && postingRef.trim().length === 0)}
            onClick={() => void submit()}
            className="rounded bg-operator px-3 py-1.5 font-medium text-surface disabled:cursor-not-allowed disabled:opacity-40"
          >
            {pending ? "Recording…" : "Record finding"}
          </button>
        </div>

        <p className="text-ink-faint">
          Recording a finding settles the dispatch in our records. It does not re-send anything, and
          there is no endpoint that would.
        </p>

        {failure !== null ? <Failure failure={failure} /> : null}
      </div>
    </li>
  );
}
