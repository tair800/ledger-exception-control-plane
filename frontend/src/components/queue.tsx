"use client";

/**
 * The exception queue.
 *
 * Filtering happens over the page the console loaded, not upstream: `GET /api/v1/exceptions` takes
 * a `limit` and nothing else, and adding query names the API would ignore would produce a filter
 * that silently did nothing. The count line says which of the two it is, so a reviewer is never
 * guessing whether "3 of 41" means the queue or the page.
 *
 * The amount column is the settlement line's amount, rendered as text. It is not summed, not
 * grouped by currency, and there is no total row — a column of mixed-currency strings has no
 * meaningful total, and computing one in a browser would be the defect this project exists to
 * prevent.
 */

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { Badge, Money } from "@/components/primitives";
import { Empty, Failure, Loading } from "@/components/states";
import { listExceptions } from "@/lib/client";
import type { ApiFailure, ExceptionSummary } from "@/lib/types";

type Stage = "all" | "awaiting_proposal" | "awaiting_decision" | "decided";

const STAGES: { value: Stage; label: string }[] = [
  { value: "all", label: "Any stage" },
  { value: "awaiting_proposal", label: "No proposal yet" },
  { value: "awaiting_decision", label: "Proposed, undecided" },
  { value: "decided", label: "Decided" },
];

function stageOf(row: ExceptionSummary): Stage {
  if (row.decided) return "decided";
  return row.has_proposal ? "awaiting_decision" : "awaiting_proposal";
}

export function Queue() {
  const [rows, setRows] = useState<ExceptionSummary[] | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [status, setStatus] = useState("all");
  const [classification, setClassification] = useState("all");
  const [stage, setStage] = useState<Stage>("all");
  const [reference, setReference] = useState("");

  const load = useCallback(async () => {
    setRows(null);
    setFailure(null);
    const result = await listExceptions();
    if (result.ok) setRows(result.data);
    else setFailure(result.failure);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const classifications = useMemo(
    () => [...new Set((rows ?? []).map((row) => row.classification))].sort(),
    [rows],
  );
  const statuses = useMemo(() => [...new Set((rows ?? []).map((row) => row.status))].sort(), [rows]);

  const filtered = useMemo(() => {
    const needle = reference.trim().toLowerCase();
    return (rows ?? []).filter((row) => {
      if (status !== "all" && row.status !== status) return false;
      if (classification !== "all" && row.classification !== classification) return false;
      if (stage !== "all" && stageOf(row) !== stage) return false;
      if (needle.length > 0) {
        const haystack = `${row.psp_reference ?? ""} ${row.correlation_id} ${row.id}`.toLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });
  }, [rows, status, classification, stage, reference]);

  if (failure !== null) return <Failure failure={failure} onRetry={() => void load()} />;
  if (rows === null) return <Loading label="Loading the exception queue…" />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Settlement exceptions</h1>
          <p className="text-ink-dim">
            Residuals the deterministic matcher could not clear. Open one to see the evidence, what
            the model proposed, who decided, and what was posted.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="rounded border border-edge bg-panel px-3 py-1.5 text-ink-dim hover:text-ink"
        >
          Reload
        </button>
      </div>

      <div className="flex flex-wrap gap-3 rounded-md border border-edge bg-panel p-3">
        <Select label="Status" value={status} onChange={setStatus} options={statuses} />
        <Select
          label="Classification"
          value={classification}
          onChange={setClassification}
          options={classifications}
        />
        <label className="flex flex-col gap-1">
          <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
            Stage
          </span>
          <select
            aria-label="Stage"
            value={stage}
            onChange={(event) => setStage(event.target.value as Stage)}
            className="rounded border border-edge bg-surface px-2 py-1.5 text-ink"
          >
            {STAGES.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-1 flex-col gap-1">
          <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
            Reference or correlation id
          </span>
          <input
            aria-label="Reference or correlation id"
            value={reference}
            onChange={(event) => setReference(event.target.value)}
            placeholder="substring match"
            className="tabular w-full rounded border border-edge bg-surface px-2 py-1.5 text-ink placeholder:text-ink-faint"
          />
        </label>
      </div>

      <p className="text-ink-dim">
        {filtered.length} of {rows.length} loaded {rows.length === 1 ? "exception" : "exceptions"}.
        Filters apply to the loaded page; the queue endpoint takes a limit and no filter parameters.
      </p>

      {rows.length === 0 ? (
        <Empty
          title="No exceptions"
          detail="Nothing has been raised on this control plane. Ingest a settlement file and run the matcher to produce residuals."
        />
      ) : filtered.length === 0 ? (
        <Empty
          title="No exception matches these filters"
          detail="Every loaded exception was excluded. Widen a filter or clear the reference search."
        />
      ) : (
        <div className="overflow-x-auto rounded-md border border-edge">
          <table className="w-full border-collapse text-left">
            <thead className="bg-panel-raised text-[11px] uppercase tracking-wider text-ink-faint">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">
                  PSP reference
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Classification
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Status
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Stage
                </th>
                <th scope="col" className="px-3 py-2 text-right font-medium">
                  Line amount
                </th>
                <th scope="col" className="px-3 py-2 font-medium">
                  Correlation id
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.id} className="border-t border-edge bg-panel hover:bg-panel-raised">
                  <td className="px-3 py-2">
                    <Link
                      href={`/exceptions/${row.id}`}
                      className="tabular text-operator underline-offset-2 hover:underline"
                    >
                      {row.psp_reference ?? row.id}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-ink-dim">{row.classification}</td>
                  <td className="px-3 py-2">
                    <Badge tone={row.status === "resolved" ? "deterministic" : "neutral"}>
                      {row.status}
                    </Badge>
                  </td>
                  <td className="px-3 py-2">
                    <StageBadge stage={stageOf(row)} />
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Money amount={row.amount} currency={row.currency} />
                  </td>
                  <td className="tabular px-3 py-2 text-ink-faint">{row.correlation_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function StageBadge({ stage }: { stage: Stage }) {
  if (stage === "decided") return <Badge tone="deterministic">decided</Badge>;
  if (stage === "awaiting_decision") return <Badge tone="model">awaiting decision</Badge>;
  return <Badge tone="neutral">no proposal</Badge>;
}

function Select({
  label,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  options: string[];
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
        {label}
      </span>
      <select
        aria-label={label}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="rounded border border-edge bg-surface px-2 py-1.5 text-ink"
      >
        <option value="all">Any {label.toLowerCase()}</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  );
}
