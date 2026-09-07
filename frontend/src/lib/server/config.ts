/**
 * Server-side configuration, read from the environment once per call.
 *
 * **This is the only module in the console permitted to turn a string into a number**, and the
 * money-arithmetic scan in `src/test/no-money-arithmetic.test.ts` allowlists it by name for exactly
 * that reason: a millisecond timeout from configuration is not a monetary value, and nothing in
 * here handles one. Keeping the single permitted coercion in a file that never sees an amount is
 * what makes the allowlist safe — any *other* file that starts coercing numbers fails the scan.
 */

const DEFAULT_BASE_URL = "http://localhost:8000";
const DEFAULT_TIMEOUT_MS = 8000;

/** Base URL of the control plane, without a trailing slash and without the `/api/v1` prefix. */
export function controlPlaneBaseUrl(): string {
  const configured = process.env.CONTROL_PLANE_BASE_URL?.trim();
  return (configured && configured.length > 0 ? configured : DEFAULT_BASE_URL).replace(/\/+$/, "");
}

/** Bounded, so a stalled control plane becomes an error state rather than a hanging page. */
export function upstreamTimeoutMs(): number {
  const raw = process.env.CONTROL_PLANE_TIMEOUT_MS?.trim();
  if (!raw) return DEFAULT_TIMEOUT_MS;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TIMEOUT_MS;
}

/**
 * Whether to mark the session cookie `Secure`.
 *
 * Off by default because a `Secure` cookie is dropped over plain http and local development would
 * silently fail to sign in. Any deployment serving the console over HTTPS must set this true.
 */
export function cookieSecure(): boolean {
  return process.env.CONSOLE_COOKIE_SECURE?.trim().toLowerCase() === "true";
}
