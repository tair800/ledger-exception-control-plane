"use client";

/**
 * The fault-injection demonstration.
 *
 * The claim this project makes is narrow and conditional: under the declared adapter contract, and
 * with a retry-independent operation identifier, a crash between the socket write and the response
 * does not produce a second financial side effect. The chaos suite demonstrates that in CI. This
 * screen exists so a visitor can watch it happen instead.
 *
 * Two things it will not do. It will not run outside demo mode — the control plane refuses a fault
 * injector on an instance doing real work, and so does this screen. And it will not report an
 * outcome it did not observe: the number that carries the demonstration is the ledger's own applied
 * count, and a console that computed "no duplicate" from the absence of a second row in *our*
 * tables would be asserting the conclusion rather than observing it.
 *
 * The control plane publishes the endpoint; an instance that does not disables the control
 * and names the missing route rather than failing on click.
 */

import { useEffect, useState } from "react";

import { useConsole } from "@/components/console-session";
import { Badge } from "@/components/primitives";
import { Failure, Loading } from "@/components/states";
import { injectCrash, listFaultTargets } from "@/lib/client";
import type { ApiFailure, FaultTargetView } from "@/lib/types";

export function DemoControl() {
  const { meta } = useConsole();
  const [candidates, setCandidates] = useState<FaultTargetView[] | null>(null);
  const [selected, setSelected] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [observed, setObserved] = useState<unknown>(null);

  // **Asked, not derived.** An earlier version listed the *undecided* exceptions, reasoning that
  // an undispatched posting must belong to one. It is the inverse: the injector needs an approved
  // decision and a priced adjustment, which is exactly what an undecided exception lacks, so every
  // default selection returned 409 on the first press. Eligibility is a precondition of the control
  // plane's endpoint, and the control plane is what publishes it.
  useEffect(() => {
    void listFaultTargets().then((result) => {
      if (result.ok) {
        setCandidates(result.data);
        setSelected(result.data[0]?.exception_id ?? "");
      } else {
        setCandidates([]);
      }
    });
  }, []);

  // Both halves: an instance publishing one and not the other would leave a control that
  // cannot be populated, or one that cannot be fired.
  const endpointMissing =
    !meta.capabilities.demo_inject_crash || !meta.capabilities.demo_fault_targets;
  const demoMode = meta.demo_mode;
  const enabled = demoMode === true && !endpointMissing && selected.length > 0 && !pending;

  async function inject() {
    setPending(true);
    setFailure(null);
    setObserved(null);
    const result = await injectCrash(selected);
    setPending(false);
    if (result.ok) setObserved(result.data);
    else setFailure(result.failure);
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Fault injection</h1>
        <p className="max-w-prose text-ink-dim">
          Crash a dispatch between the socket write and the response, then let the system recover,
          and read the ledger&apos;s own applied count.
        </p>
      </div>

      <div className="rounded-md border border-edge bg-panel px-4 py-3.5">
        <h2 className="font-semibold text-ink">Instance state</h2>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-ink-dim">Demo mode:</span>
          {demoMode === true ? (
            <Badge tone="operator">enabled</Badge>
          ) : demoMode === false ? (
            <Badge tone="refusal">disabled</Badge>
          ) : (
            <Badge tone="model">unknown</Badge>
          )}
          <span className="text-ink-dim">Fault endpoint:</span>
          {endpointMissing ? (
            <Badge tone="neutral">not implemented</Badge>
          ) : (
            <Badge tone="deterministic">available</Badge>
          )}
        </div>

        {demoMode === "unknown" ? (
          <p className="mt-3 text-ink-dim">
            This control plane publishes nothing that reports whether it is a demonstration
            instance, so the console cannot tell. It treats that as its own state rather than
            assuming: &ldquo;not a demo&rdquo; and &ldquo;nobody told me&rdquo; look identical to
            you and only one of them is true, so the control stays disabled.
          </p>
        ) : null}

        {endpointMissing ? (
          <p className="mt-3 text-ink-dim">
            This control plane publishes no fault-injection endpoint. The crash scenarios still
            run in the committed chaos suite against three adapter capability configurations; only
            the demo-mode HTTP control is absent from this instance. The console reads the published
            endpoint list, so the control enables itself when the route appears — no change here.
          </p>
        ) : null}
      </div>

      <div className="rounded-md border border-edge bg-panel px-4 py-3.5">
        <h2 className="font-semibold text-ink">Inject a crash</h2>
        <p className="mt-1 max-w-prose text-ink-dim">
          Pick an approved, priced posting still awaiting its first dispatch — the control plane
          publishes which those are. The injected fault is a crash after the socket write and before
          the response is read: the case where the system cannot know whether the ledger applied the
          instruction.
        </p>

        {candidates === null ? (
          <div className="mt-3">
            <Loading label="Loading candidate exceptions…" />
          </div>
        ) : (
          <div className="mt-3 flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1">
              <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
                Exception
              </span>
              <select
                aria-label="Exception"
                value={selected}
                onChange={(event) => setSelected(event.target.value)}
                disabled={candidates.length === 0}
                className="tabular rounded border border-edge bg-surface px-2 py-1.5 text-ink disabled:opacity-40"
              >
                {candidates.length === 0 ? (
                  <option value="">no posting is awaiting a first dispatch</option>
                ) : (
                  candidates.map((row) => (
                    <option key={row.exception_id} value={row.exception_id}>
                      {row.psp_reference ?? row.exception_id} · {row.classification}
                    </option>
                  ))
                )}
              </select>
            </label>
            <button
              type="button"
              disabled={!enabled}
              title={
                endpointMissing
                  ? "This control plane publishes no demo fault-injection endpoint."
                  : demoMode !== true
                    ? "Fault injection runs only on an instance configured as a demonstration."
                    : undefined
              }
              onClick={() => void inject()}
              className="rounded bg-operator px-3 py-1.5 font-medium text-surface disabled:cursor-not-allowed disabled:opacity-40"
            >
              {pending ? "Injecting…" : "Crash after the socket write"}
            </button>
          </div>
        )}

        {failure !== null ? (
          <div className="mt-3">
            <Failure failure={failure} />
          </div>
        ) : null}

        {observed !== null ? (
          <div className="mt-3 rounded border border-deterministic/40 bg-deterministic-bg px-3 py-2.5">
            <p className="font-semibold text-deterministic">Observed outcome</p>
            <p className="mt-1 text-ink-dim">
              Reported by the control plane, unmodified. The applied count is the simulated
              ledger&apos;s own, not a count of rows in our tables.
            </p>
            <pre className="tabular mt-2 overflow-x-auto whitespace-pre-wrap text-ink">
              {JSON.stringify(observed, null, 2)}
            </pre>
          </div>
        ) : null}
      </div>

      <div className="rounded-md border border-edge bg-panel px-4 py-3.5">
        <h2 className="font-semibold text-ink">What the demonstration shows</h2>
        <ul className="mt-2 max-w-prose list-disc space-y-1.5 pl-5 text-ink-dim">
          <li>
            A write-ahead attempt row is committed before the socket write, so a crash mid-send is
            recoverable as an ambiguous outcome rather than invisible.
          </li>
          <li>
            The operation identifier is derived from the instruction payload alone — no attempt
            counter, no timestamp, no random value, no approver identity — so a re-send carries the
            same key and the ledger recognises the repeat.
          </li>
          <li>
            The effect is <em>effectively-once</em>, and that claim is conditional on the adapter
            declaring that it enforces the key or can answer a posting-identity query. Where the
            adapter does not meet that bar the claim is withdrawn, not reworded: the outcome stays
            ambiguous, nothing is retried, and the operation goes to manual recovery.
          </li>
          <li>
            The transactional outbox is at-least-once. It guarantees the intent is not lost, never
            that it was delivered once. Those are different properties and conflating them is the
            usual way this goes wrong.
          </li>
        </ul>
      </div>
    </div>
  );
}
