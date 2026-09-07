/**
 * Loading, empty and failure, as three explicit components.
 *
 * They exist as components rather than as inline JSX because every screen owes all three and the
 * distinctions matter here more than usual: an empty dead-letter queue and a dead-letter queue an
 * analyst is not allowed to see look the same if you let them, and one of those means "nothing
 * failed" while the other means "you cannot know".
 */

import type { ApiFailure } from "@/lib/types";

export function Loading({ label }: { label: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex items-center gap-3 rounded-md border border-edge bg-panel px-4 py-6 text-ink-dim"
    >
      <span
        aria-hidden
        className="h-3 w-3 animate-pulse rounded-full bg-operator"
      />
      <span>{label}</span>
    </div>
  );
}

export function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="rounded-md border border-dashed border-edge bg-panel px-4 py-8 text-center">
      <p className="font-medium text-ink">{title}</p>
      <p className="mx-auto mt-1 max-w-prose text-ink-dim">{detail}</p>
    </div>
  );
}

/**
 * A failure the console can name.
 *
 * A 401 or 403 is rendered as an *authority* message, not as an error: the request worked, the
 * control plane understood it, and the answer was that this principal may not do that. Calling it
 * an error would invite the operator to retry something that will always refuse.
 */
export function Failure({ failure, onRetry }: { failure: ApiFailure; onRetry?: () => void }) {
  const authority = failure.authority;
  const notImplemented = failure.not_implemented;

  const tone = authority
    ? "border-model/40 bg-model-bg text-model"
    : notImplemented
      ? "border-edge bg-panel-raised text-ink-dim"
      : "border-refusal/40 bg-refusal-bg text-refusal";

  const heading = authority
    ? "Insufficient authority"
    : notImplemented
      ? "Not available on this control plane"
      : "Request failed";

  return (
    <div role="alert" className={`rounded-md border px-4 py-4 ${tone}`}>
      <p className="font-semibold">{heading}</p>
      <p className="mt-1 max-w-prose text-ink-dim">{failure.message}</p>
      {failure.reason ? (
        <p className="tabular mt-2 text-ink-faint">reason: {failure.reason}</p>
      ) : null}
      {onRetry && !authority && !notImplemented ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 rounded border border-edge bg-panel px-3 py-1.5 text-ink hover:bg-panel-raised"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}
