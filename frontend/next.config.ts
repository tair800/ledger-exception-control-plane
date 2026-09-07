import type { NextConfig } from "next";

/**
 * The console is a Next.js app that talks to the control plane through its own route handlers
 * (`src/app/api/console/**`) rather than from the browser directly.
 *
 * That is a security decision, not a convenience: the control plane authenticates with a bearer
 * token, and a browser-side call would mean holding that token in JavaScript the page also renders
 * untrusted-ish content into. The token lives in an httpOnly cookie instead, is attached
 * server-side, and never reaches client code. It also means the browser only ever talks to its own
 * origin, so the backend needs no CORS allowlist for the console to work (`Settings.cors_allow_origins`
 * is fail-closed and empty by default).
 */
const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
};

export default nextConfig;
