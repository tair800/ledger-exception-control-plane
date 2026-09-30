"use client";

/**
 * The console's session and what it knows about the connected control plane.
 *
 * Sign-in is a bearer token typed once. It is posted to this app's own route handler, validated
 * against the control plane, and stored in an httpOnly cookie — so from here on the token is not
 * readable by any script on the page, including this one. `useConsole()` therefore returns the
 * *principal and role* and never the credential.
 *
 * On the public demonstration, the sign-in screen also offers the three published demo roles as
 * buttons. A button sends the *role name*; the server supplies that role's published token and
 * validates it the same way, so the credential still never reaches the page.
 *
 * When the control plane publishes no identity endpoint the role is `null` and `authority` is
 * `"unverified"`. Screens must treat that as its own state: the console will not guess a role, and
 * it says as much where an operator can see it, because a console that displayed a role nobody
 * confirmed would be making the same mistake the API refuses to make.
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import {
  readDemoSignIn,
  readMeta,
  readSession,
  signIn,
  signInAsDemoRole,
  signOut,
  type Result,
} from "@/lib/client";
import { Failure, Loading } from "@/components/states";
import type { ApiFailure, ConsoleMeta, ConsoleSession, DemoSignIn } from "@/lib/types";

interface ConsoleState {
  session: ConsoleSession;
  meta: ConsoleMeta;
  signOut: () => void;
}

const UNKNOWN_META: ConsoleMeta = {
  demo_mode: "unknown",
  version: null,
  reachable: false,
  capabilities: {
    dlq_replay: false,
    demo_inject_crash: false,
    demo_fault_targets: false,
    demo_reset: false,
    identity: false,
    meta: false,
    request_edit: false,
  },
};

const ConsoleContext = createContext<ConsoleState | null>(null);

export function useConsole(): ConsoleState {
  const state = useContext(ConsoleContext);
  if (state === null) throw new Error("useConsole used outside the console session provider");
  return state;
}

export function ConsoleSessionProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<ConsoleSession | null>(null);
  const [meta, setMeta] = useState<ConsoleMeta>(UNKNOWN_META);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  const load = useCallback(async () => {
    setFailure(null);
    const result = await readSession();
    if (!result.ok) {
      setFailure(result.failure);
      return;
    }
    setSession(result.data);
    if (result.data.signed_in) {
      const instance = await readMeta();
      setMeta(instance.ok ? instance.data : UNKNOWN_META);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const end = useCallback(() => {
    void signOut().then(() => {
      setSession({ signed_in: false, authority: "unverified", principal: null, role: null });
      setMeta(UNKNOWN_META);
    });
  }, []);

  if (failure !== null) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <Failure failure={failure} onRetry={() => void load()} />
      </div>
    );
  }

  if (session === null) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <Loading label="Opening the console…" />
      </div>
    );
  }

  if (!session.signed_in) {
    return <SignIn onSignedIn={(next) => void afterSignIn(next, setSession, setMeta)} />;
  }

  return (
    <ConsoleContext.Provider value={{ session, meta, signOut: end }}>
      {children}
    </ConsoleContext.Provider>
  );
}

async function afterSignIn(
  next: ConsoleSession,
  setSession: (value: ConsoleSession) => void,
  setMeta: (value: ConsoleMeta) => void,
) {
  setSession(next);
  const instance = await readMeta();
  setMeta(instance.ok ? instance.data : UNKNOWN_META);
}

type DemoRole = DemoSignIn["roles"][number];

/** What each demonstration role may and may not do, as the control plane enforces it. */
const DEMO_ROLE_COPY: Record<DemoRole, { label: string; detail: string }> = {
  analyst: {
    label: "Analyst",
    detail: "Read everything and reject a treatment. Cannot authorise a posting.",
  },
  operator: {
    label: "Operator",
    detail: "Work the dead-letter and recovery queues and the fault-injection control. Cannot approve.",
  },
  controller: {
    label: "Controller",
    detail: "Approve or edit a treatment. Cannot work the failure queues.",
  },
};

/**
 * How long a sign-in keeps waiting for a control plane that is waking, and how often it asks.
 * Gentle on purpose: a host starting an instance answers 429 to a client that asks too often.
 */
const WAKE_LIMIT_MS = 150_000;
const WAKE_POLL_MS = 5_000;
/** After this many seconds of waiting, the screen says the backend is waking. */
const WAKE_NOTICE_SECONDS = 3;

/**
 * A failure that means the free-tier control plane is still starting, not that sign-in failed:
 * nothing answered in time, or its host answered 429/502/503/504 while the container starts.
 */
const WAKING_STATUSES = new Set([0, 429, 502, 503, 504]);

function isWaking(failure: ApiFailure): boolean {
  return WAKING_STATUSES.has(failure.status);
}

const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

/**
 * Sign in, waiting out a control plane that is waking from free-tier sleep.
 *
 * Bounded by `WAKE_LIMIT_MS`; any other failure — an unknown token, a refusal — is returned at once.
 */
async function signInWaiting(
  attempt: () => Promise<Result<ConsoleSession>>,
): Promise<Result<ConsoleSession>> {
  const startedAt = Date.now();
  for (;;) {
    const result = await attempt();
    if (result.ok || !isWaking(result.failure) || Date.now() - startedAt >= WAKE_LIMIT_MS) {
      return result;
    }
    await pause(WAKE_POLL_MS);
  }
}

/** Seconds spent waiting, counted by a ticking timer rather than by arithmetic on timestamps. */
function useElapsedSeconds(running: boolean): number {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    if (!running) {
      setSeconds(0);
      return;
    }
    const timer = setInterval(() => setSeconds((value) => value + 1), 1_000);
    return () => clearInterval(timer);
  }, [running]);
  return seconds;
}

function SignIn({ onSignedIn }: { onSignedIn: (session: ConsoleSession) => void }) {
  const [token, setToken] = useState("");
  const [pending, setPending] = useState<DemoRole | "token" | null>(null);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const [demo, setDemo] = useState<DemoSignIn | null>(null);
  const seconds = useElapsedSeconds(pending !== null);

  useEffect(() => {
    void readDemoSignIn().then((result) => {
      setDemo(result.ok ? result.data : { enabled: false, roles: [] });
    });
  }, []);

  const run = (which: DemoRole | "token", attempt: () => Promise<Result<ConsoleSession>>) => {
    setPending(which);
    setFailure(null);
    void signInWaiting(attempt).then((result) => {
      setPending(null);
      if (result.ok) onSignedIn(result.data);
      else setFailure(result.failure);
    });
  };

  const offersDemo = demo !== null && demo.enabled && demo.roles.length > 0;

  const tokenForm = (
    <form
      className="space-y-3 rounded-md border border-edge bg-panel p-4"
      onSubmit={(event) => {
        event.preventDefault();
        run("token", () => signIn(token));
      }}
    >
      <label className="block" htmlFor="token">
        <span className="text-[11px] font-medium uppercase tracking-wider text-ink-faint">
          Control-plane bearer token
        </span>
        <input
          id="token"
          name="token"
          type="password"
          autoComplete="off"
          value={token}
          onChange={(event) => setToken(event.target.value)}
          placeholder="paste the token for your principal"
          className="tabular mt-1.5 w-full rounded border border-edge bg-surface px-3 py-2 text-ink placeholder:text-ink-faint"
        />
      </label>

      <p className="text-ink-dim">
        The token is validated against the control plane and kept in an httpOnly cookie. It is
        never held in browser storage and never reaches page scripts. Your role — and therefore
        which controls this console shows you — is decided by the control plane, not here.
      </p>

      <button
        type="submit"
        disabled={pending !== null || token.trim().length === 0}
        className="w-full rounded bg-operator px-3 py-2 font-medium text-surface disabled:cursor-not-allowed disabled:opacity-40"
      >
        {pending === "token" ? "Validating…" : "Sign in"}
      </button>
    </form>
  );

  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center gap-5 p-8">
      <div>
        <h1 className="text-xl font-semibold">Ledger exception control plane</h1>
        <p className="mt-1 text-ink-dim">Operations console</p>
      </div>

      {offersDemo ? (
        <section
          aria-labelledby="demo-roles-heading"
          className="space-y-3 rounded-md border border-operator/40 bg-panel p-4"
          data-testid="demo-roles"
        >
          <div>
            <p className="text-[11px] font-medium uppercase tracking-wider text-operator">
              Public synthetic demo roles
            </p>
            <h2 id="demo-roles-heading" className="mt-1 font-semibold text-ink">
              Explore the console as one of its three roles
            </h2>
            <p className="mt-1 text-ink-dim">
              No credentials needed. These are public demonstration roles, not real accounts: every
              row is synthetic and the ledger is simulated. The control plane decides what each role
              may do, exactly as it would for a real principal.
            </p>
          </div>
          <ul className="space-y-2">
            {demo.roles.map((role) => (
              <li key={role}>
                <button
                  type="button"
                  disabled={pending !== null}
                  onClick={() => run(role, () => signInAsDemoRole(role))}
                  className="w-full rounded border border-edge bg-surface px-3 py-2.5 text-left hover:border-operator disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <span className="block font-medium text-ink">
                    {pending === role ? "Signing in…" : `Explore as ${DEMO_ROLE_COPY[role].label}`}
                  </span>
                  <span className="mt-0.5 block text-ink-dim">{DEMO_ROLE_COPY[role].detail}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {pending !== null && seconds >= WAKE_NOTICE_SECONDS ? (
        <div
          role="status"
          aria-live="polite"
          className="rounded-md border border-edge bg-panel px-4 py-3"
          data-testid="waking-notice"
        >
          <p className="font-medium text-ink">Starting the demo backend…</p>
          <p className="mt-1 text-ink-dim">
            The control plane runs on a free tier that sleeps after inactivity and can take up to a
            minute to wake. You will be signed in automatically as soon as it answers.
          </p>
          <p className="tabular mt-1 text-ink-faint">waiting · {seconds}s</p>
        </div>
      ) : null}

      {offersDemo ? (
        <details className="rounded-md border border-edge bg-panel">
          <summary className="cursor-pointer px-4 py-3 text-ink-dim">
            Sign in with a bearer token instead
          </summary>
          <div className="px-4 pb-4">{tokenForm}</div>
        </details>
      ) : (
        tokenForm
      )}

      {failure !== null ? <Failure failure={failure} /> : null}
    </main>
  );
}
