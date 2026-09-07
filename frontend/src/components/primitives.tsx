/**
 * Small display primitives shared by the screens.
 *
 * `Money` is the important one, and the comment on it is the point of the whole component: it takes
 * the string the API returned and renders it. There is no formatting option, no locale, no
 * grouping, no sign handling and no currency conversion, because every one of those is arithmetic
 * on a monetary value performed in a browser, and this project exists to demonstrate that money is
 * computed in exactly one place by deterministic code that is tested.
 */

import type { ReactNode } from "react";

/** A labelled fact. `null` renders as a named gap rather than as blank space. */
export function Field({
  label,
  children,
  mono = false,
  span = false,
}: {
  label: string;
  children: ReactNode;
  mono?: boolean;
  span?: boolean;
}) {
  const empty = children === null || children === undefined || children === "";
  return (
    <div className={span ? "sm:col-span-2" : undefined}>
      <dt className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">{label}</dt>
      <dd className={`mt-0.5 break-words ${mono ? "tabular" : ""} ${empty ? "text-ink-faint" : "text-ink"}`}>
        {empty ? "not recorded" : children}
      </dd>
    </div>
  );
}

/**
 * A monetary value, exactly as the control plane sent it.
 *
 * The `amount` prop is typed `string | null | undefined` and there is no numeric overload. Currency
 * is rendered beside it and never combined with it: two values in different currencies are two
 * facts, never a total.
 */
export function Money({
  amount,
  currency,
  size = "base",
}: {
  amount: string | null | undefined;
  currency: string | null | undefined;
  size?: "base" | "large";
}) {
  if (amount === null || amount === undefined || amount === "") {
    return <span className="text-ink-faint">no amount</span>;
  }
  return (
    <span className={`tabular ${size === "large" ? "text-lg font-semibold" : ""} text-ink`}>
      <span>{amount}</span>
      {currency ? <span className="ml-1.5 text-ink-dim">{currency}</span> : null}
    </span>
  );
}

export type BadgeTone = "neutral" | "model" | "deterministic" | "refusal" | "operator";

const TONES: Record<BadgeTone, string> = {
  neutral: "border-edge bg-panel-raised text-ink-dim",
  model: "border-model/40 bg-model-bg text-model",
  deterministic: "border-deterministic/40 bg-deterministic-bg text-deterministic",
  refusal: "border-refusal/40 bg-refusal-bg text-refusal",
  operator: "border-operator/40 bg-operator-bg text-operator",
};

export function Badge({ tone = "neutral", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Panel({
  title,
  subtitle,
  provenance,
  children,
}: {
  title: string;
  subtitle?: string;
  /** Who produced the contents: shown as a badge so the boundary is visible, not inferred. */
  provenance?: { label: string; tone: BadgeTone };
  children: ReactNode;
}) {
  return (
    <section className="rounded-md border border-edge bg-panel">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-edge px-4 py-2.5">
        <div>
          <h2 className="font-semibold text-ink">{title}</h2>
          {subtitle ? <p className="mt-0.5 text-ink-dim">{subtitle}</p> : null}
        </div>
        {provenance ? <Badge tone={provenance.tone}>{provenance.label}</Badge> : null}
      </header>
      <div className="px-4 py-3.5">{children}</div>
    </section>
  );
}

/** A timestamp rendered as the ISO string the API sent. Not reformatted, so it never shifts zone. */
export function Timestamp({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-ink-faint">not recorded</span>;
  return <span className="tabular text-ink-dim">{value}</span>;
}
