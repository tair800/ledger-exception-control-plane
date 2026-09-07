"use client";

/**
 * The console's session and what it knows about the connected control plane.
 *
 * Sign-in is a bearer token typed once. It is posted to this app's own route handler, validated
 * against the control plane, and stored in an httpOnly cookie — so from here on the token is not
 * readable by any script on the page, including this one. `useConsole()` therefore returns the
 * *principal and role* and never the credential.
 *
 * When the control plane publishes no identity endpoint the role is `null` and `authority` is
 * `"unverified"`. Screens must treat that as its own state: the console will not guess a role, and
 * it says as much where an operator can see it, because a console that displayed a role nobody
 * confirmed would be making the same mistake the API refuses to make.
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { readMeta, readSession, signIn, signOut } from "@/lib/client";
import { Failure, Loading } from "@/components/states";
import type { ApiFailure, ConsoleMeta, ConsoleSession } from "@/lib/types";

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

function SignIn({ onSignedIn }: { onSignedIn: (session: ConsoleSession) => void }) {
  const [token, setToken] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);

  return (
    <main className="mx-auto flex min-h-screen max-w-lg flex-col justify-center gap-5 p-8">
      <div>
        <h1 className="text-xl font-semibold">Ledger exception control plane</h1>
        <p className="mt-1 text-ink-dim">Operations console</p>
      </div>

      <form
        className="space-y-3 rounded-md border border-edge bg-panel p-4"
        onSubmit={(event) => {
          event.preventDefault();
          setPending(true);
          setFailure(null);
          void signIn(token).then((result) => {
            setPending(false);
            if (result.ok) onSignedIn(result.data);
            else setFailure(result.failure);
          });
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
          disabled={pending || token.trim().length === 0}
          className="w-full rounded bg-operator px-3 py-2 font-medium text-surface disabled:cursor-not-allowed disabled:opacity-40"
        >
          {pending ? "Validating…" : "Sign in"}
        </button>
      </form>

      {failure !== null ? <Failure failure={failure} /> : null}
    </main>
  );
}
