"use client";

/**
 * The dead-letter queue: dispatches that exhausted a bounded retry budget.
 *
 * There is no amount in this view, and that is a schema guarantee rather than a layout choice — the
 * `dlq` table's envelope is JSONB and a check constraint rejects amount-like keys in it, because
 * money in JSONB would bypass the money constraints everywhere else. The amount is reconstructed
 * from the adjustment at replay time, so this screen shows an operator *what failed and why* and
 * never invites them to re-price it. The link back to the exception is how you see the amount.
 *
 * Replay is shipped. The console still asks the
 * control plane whether the endpoint exists rather than hard-coding the answer, so it enables
 * itself the moment the route is added.
 */

import { useCallback, useEffect, useState } from "react";

import { useConsole } from "@/components/console-session";
import { Badge, Timestamp } from "@/components/primitives";
import { Empty, Failure, Loading } from "@/components/states";
import { listDeadLetters, replayDeadLetter } from "@/lib/client";
import type { ApiFailure, DeadLetterView } from "@/lib/types";

export function DeadLetterQueue() {
  const { meta } = useConsole();
  const [rows, setRows] = useState<DeadLetterView[] | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [pendingOnly, setPendingOnly] = useState(true);
  const [replayFailure, setReplayFailure] = useState<ApiFailure | null>(null);
  const [replaying, setReplaying] = useState<string | null>(null);

  const load = useCallback(async () => {
    setRows(null);
    setFailure(null);
    const result = await listDeadLetters(pendingOnly);
    if (result.ok) setRows(result.data);
    else setFailure(result.failure);
  }, [pendingOnly]);

  useEffect(() => {
    void load();
  }, [load]);

  const replayAvailable = meta.capabilities.dlq_replay;

  async function replay(id: string) {
    setReplaying(id);
    setReplayFailure(null);
    const result = await replayDeadLetter(id);
    setReplaying(null);
    if (result.ok) void load();
    else setReplayFailure(result.failure);
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Dead letters</h1>
          <p className="max-w-prose text-ink-dim">
            Dispatches that exhausted their retry budget. Retries apply only to transport failures
            where no byte was written; anything ambiguous goes to recovery instead, never here.
          </p>
        </div>
        <label className="flex items-center gap-2 text-ink-dim">
          <input
            type="checkbox"
            checked={pendingOnly}
            onChange={(event) => setPendingOnly(event.target.checked)}
            className="h-4 w-4"
          />
          Pending only
        </label>
      </div>

      {!replayAvailable ? (
        <p className="rounded border border-edge bg-panel-raised px-3 py-2.5 text-ink-dim">
          <span className="font-medium text-ink">Replay is not available on this control plane.</span>{" "}
          Replay is also a command-line tool, and this deployment publishes no HTTP endpoint for it,
          so the button below is disabled. It enables itself when the control plane publishes{" "}
          <code className="tabular">POST /api/v1/dlq/{"{dlq_id}"}/replay</code> — the console reads
          the endpoint list rather than assuming.
        </p>
      ) : null}

      {replayFailure !== null ? <Failure failure={replayFailure} /> : null}

      {failure !== null ? (
        <Failure failure={failure} onRetry={() => void load()} />
      ) : rows === null ? (
        <Loading label="Loading the dead-letter queue…" />
      ) : rows.length === 0 ? (
        <Empty
          title={pendingOnly ? "No pending dead letters" : "No dead letters"}
          detail={
            pendingOnly
              ? "Nothing is waiting to be replayed. Clear the filter to see entries that were already replayed or abandoned."
              : "No dispatch on this control plane has exhausted its retry budget."
          }
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-edge">
          <table className="w-full border-collapse text-left">
            <thead className="bg-panel-raised text-[11px] uppercase tracking-wider text-ink-faint">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  Operation id
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Reason
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Attempts
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Replay state
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Dead-lettered at
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Replay
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="border-t border-edge bg-panel">
                  <td className="tabular px-3 py-2 text-ink">{row.operation_id || "not recorded"}</td>
                  <td className="px-3 py-2 text-ink-dim">{row.reason}</td>
                  <td className="tabular px-3 py-2">{row.attempts}</td>
                  <td className="px-3 py-2">
                    <Badge
                      tone={
                        row.replay_state === "replayed"
                          ? "deterministic"
                          : row.replay_state === "abandoned"
                            ? "refusal"
                            : "neutral"
                      }
                    >
                      {row.replay_state}
                    </Badge>
                    {row.replayed_at ? (
                      <span className="tabular ml-2 text-ink-faint">{row.replayed_at}</span>
                    ) : null}
                  </td>
                  <td className="px-3 py-2">
                    <Timestamp value={row.created_at} />
                  </td>
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      disabled={
                        !replayAvailable || replaying !== null || row.replay_state !== "pending"
                      }
                      title={
                        replayAvailable
                          ? undefined
                          : "This control plane publishes no replay endpoint."
                      }
                      onClick={() => void replay(row.id)}
                      className="rounded border border-edge px-2.5 py-1 text-ink-dim enabled:hover:text-ink disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      {replaying === row.id ? "Replaying…" : "Replay"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
