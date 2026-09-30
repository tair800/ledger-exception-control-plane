/**
 * The public demonstration's three principals, offered as one-click roles.
 *
 * **These are the demonstration tokens the README and the `Makefile` already publish** — they reach
 * a disposable database of synthetic rows behind a simulated ledger, and nothing else. Offering
 * them as buttons changes how a visitor signs in, not what signing in means:
 *
 * - The browser sends a *role name*. The token stays on this server and never reaches page scripts,
 *   the same promise the typed-token sign-in makes.
 * - The token is validated against the control plane exactly as a typed one is, through
 *   `establishSession`, and the role the console shows is the one the control plane reports.
 * - Authority is untouched. A demo principal can do exactly what that principal could do before.
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
