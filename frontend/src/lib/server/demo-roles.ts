/**
 * The public demonstration's three principals, offered as one-click roles.
 *
 * **These are the demonstration tokens the README and the `Makefile` already publish** — they reach
 * a disposable database of synthetic rows behind a simulated ledger, and nothing else. Offering
 * them as buttons changes how a visitor signs in, not what signing in means:
 *
 * - The browser sends a *role name*. The token goes from this table to `establishSession`, which
 *   keeps it in the httpOnly session cookie — never in a response body, and never where a page
 *   script can read it — the same session a typed token gets.
 * - The token is validated against the control plane exactly as a typed one is, through
 *   `establishSession`, and the role the console shows is the one the control plane reports.
 * - Authority is untouched. A demo principal can do exactly what that principal could do before.
 *
 * **This is the console's only copy, and the `Makefile` is the authority it must match.** The
 * `Makefile` holds the registry that makes these tokens valid. `src/test/demo-tokens.test.ts` fails
 * if a token here stops matching the registry entry for the same role, if any other console module
 * holds one, or if anything but a route handler imports this directory — which is what keeps the
 * table out of every bundle Next.js sends to a browser.
 *
 * **Compiled in, deliberately, rather than read from configuration.** A token taken from the
 * environment would let this route present whatever credential a deployment put there, including
 * a real one. As constants it can only ever present these three.
 *
 * Off unless `CONSOLE_DEMO_SIGN_IN=true`, which only the public demonstration's deployment sets. A
 * console pointed at any other control plane offers no buttons, and even if the flag were set the
 * tokens would fail validation there, because only the demonstration's registry holds them.
 */

export const DEMO_ROLES = ["analyst", "operator", "controller"] as const;

export type DemoRole = (typeof DEMO_ROLES)[number];

const DEMO_TOKENS: Record<DemoRole, string> = {
  analyst: "demo-analyst",
  operator: "demo-operator",
  controller: "demo-controller",
};

export function demoSignInEnabled(): boolean {
  return process.env.CONSOLE_DEMO_SIGN_IN?.trim().toLowerCase() === "true";
}

export function isDemoRole(value: unknown): value is DemoRole {
  return typeof value === "string" && (DEMO_ROLES as readonly string[]).includes(value);
}

/** The published token for a demo role, or null when demo sign-in is off or the role is unknown. */
export function demoToken(role: unknown): string | null {
  if (!demoSignInEnabled() || !isDemoRole(role)) return null;
  return DEMO_TOKENS[role];
}
