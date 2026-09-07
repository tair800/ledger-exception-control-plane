"use client";

/**
 * The console's one navigation bar: four screens, the signed-in principal, and the instance.
 *
 * The operations queues are shown to every role rather than hidden from non-operators. That is
 * deliberate and mirrors the API: seeing that an operation is stuck is not the same authority as
 * judging what happened to it, and an analyst who cannot see the queue cannot escalate it either.
 * The refusal, when it comes, is an authority message on the screen itself.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { useConsole } from "@/components/console-session";
import { Badge } from "@/components/primitives";
import { ROLE_SUMMARY } from "@/lib/roles";

const SCREENS = [
  { href: "/", label: "Exceptions" },
  { href: "/dlq", label: "Dead letters" },
  { href: "/recovery", label: "Recovery" },
  { href: "/demo", label: "Demo" },
] as const;

export function Nav() {
  const { session, meta, signOut } = useConsole();
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-10 border-b border-edge bg-surface/95 backdrop-blur">
      <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2.5">
        <Link href="/" className="font-semibold text-ink">
          Ledger exception control plane
        </Link>

        <nav aria-label="Console" className="flex flex-wrap gap-1">
          {SCREENS.map((screen) => {
            const active =
              screen.href === "/" ? pathname === "/" : (pathname?.startsWith(screen.href) ?? false);
            return (
              <Link
                key={screen.href}
                href={screen.href}
                aria-current={active ? "page" : undefined}
                className={`rounded px-2.5 py-1 ${
                  active ? "bg-panel-raised text-ink" : "text-ink-dim hover:text-ink"
                }`}
              >
                {screen.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          {meta.demo_mode === true ? <Badge tone="operator">demo instance</Badge> : null}
          {meta.version ? <span className="tabular text-ink-faint">v{meta.version}</span> : null}

          {session.role !== null ? (
            <span title={ROLE_SUMMARY[session.role]}>
              <Badge tone="deterministic">{session.role}</Badge>
            </span>
          ) : (
            <span title="This control plane publishes no identity endpoint, so the console cannot confirm a role.">
              <Badge tone="model">role unverified</Badge>
            </span>
          )}

          {session.principal ? (
            <span className="tabular text-ink-dim">{session.principal}</span>
          ) : null}

          <button
            type="button"
            onClick={signOut}
            className="rounded border border-edge px-2.5 py-1 text-ink-dim hover:text-ink"
          >
            Sign out
          </button>
        </div>
      </div>

      {session.authority === "unverified" ? (
        <p className="border-t border-model/30 bg-model-bg px-4 py-1.5 text-center text-model">
          Role unverified: this control plane publishes no <code className="tabular">GET /api/v1/me</code>, so
          the console cannot know which actions your token holds. Controls are shown; the control
          plane still decides, and a refusal will be reported as one.
        </p>
      ) : null}
    </header>
  );
}
