/**
 * Establishing a console session from a bearer token — the one path every sign-in takes.
 *
 * Shared by the typed-token sign-in and the public demo's one-click roles, so that both validate
 * the same way: the token is checked against the control plane *before* the cookie is set, it is
 * stored only in an httpOnly cookie, and the role shown is the one the control plane reports.
 */

import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { SESSION_COOKIE, callControlPlane } from "@/lib/server/backend";
import { cookieSecure } from "@/lib/server/config";
import { probeCapabilities } from "@/lib/server/capabilities";
import { ROLES, type ConsoleSession, type ExceptionSummary, type Role } from "@/lib/types";

interface IdentityResponse {
  principal?: unknown;
  role?: unknown;
}

/** Read the identity endpoint if it exists. Refuses to invent a role from anything else. */
export async function identity(
  token?: string,
): Promise<{ principal: string | null; role: Role | null }> {
  const capabilities = await probeCapabilities();
  if (!capabilities.identity) return { principal: null, role: null };

  const response = await callControlPlane<IdentityResponse>("/api/v1/me", { token });
  if (!response.ok) return { principal: null, role: null };

  const principal = typeof response.data.principal === "string" ? response.data.principal : null;
  const role =
    typeof response.data.role === "string" && (ROLES as readonly string[]).includes(response.data.role)
      ? (response.data.role as Role)
      : null;
  return { principal, role };
}

/**
 * Validate `token` against the control plane and, only if it is accepted, set the session cookie.
 *
 * Validation is a read of the exception queue, which every configured role may perform, so a typo
 * is a sign-in error rather than a console that renders empty and blames the backend.
 */
export async function establishSession(token: string): Promise<NextResponse> {
  const probe = await callControlPlane<ExceptionSummary[]>("/api/v1/exceptions", {
    query: { limit: "1" },
    token,
  });
  if (!probe.ok) return NextResponse.json(probe.failure, { status: probe.failure.status || 502 });

  const jar = await cookies();
  jar.set(SESSION_COOKIE, token, {
    httpOnly: true,
    sameSite: "strict",
    secure: cookieSecure(),
    path: "/",
  });

  const { principal, role } = await identity(token);
  const session: ConsoleSession = {
    signed_in: true,
    authority: role === null ? "unverified" : "verified",
    principal,
    role,
  };
  return NextResponse.json(session);
}
