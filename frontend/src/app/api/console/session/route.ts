/**
 * The console session: a bearer token in an httpOnly cookie, and what the control plane says it is.
 *
 * `POST` validates the token against the control plane *before* setting the cookie, so a typo is a
 * sign-in error rather than a console that renders empty and blames the backend. Validation is a
 * read of the exception queue, which every configured role may perform.
 *
 * `GET` reports the session. Where the control plane publishes `GET /api/v1/me` the principal and
 * role come from it and authority is `"verified"`. Where it does not, authority is `"unverified"`
 * and the console says so on every screen: it will not name a role the server did not confirm,
 * because a role the client picked is the exact thing `routes.py` refuses to accept.
 */

import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { SESSION_COOKIE, callControlPlane, sessionToken } from "@/lib/server/backend";
import { cookieSecure } from "@/lib/server/config";
import { probeCapabilities } from "@/lib/server/capabilities";
import { ROLES, type ConsoleSession, type ExceptionSummary, type Role } from "@/lib/types";

// Vercel's Hobby plan caps a function at 10 seconds by default and 60 with this set. The cap
// matters here because the control plane runs on a free tier that scales to zero: the first
// request after an idle period waits for a container to start, which is tens of seconds, not
// milliseconds. At the default the console would time out on every cold start and show an error
// for a backend that was merely asleep.
export const maxDuration = 60;

const SIGNED_OUT: ConsoleSession = {
  signed_in: false,
  authority: "unverified",
  principal: null,
  role: null,
};

interface IdentityResponse {
  principal?: unknown;
  role?: unknown;
}

/** Read the identity endpoint if it exists. Refuses to invent a role from anything else. */
async function identity(token?: string): Promise<{ principal: string | null; role: Role | null }> {
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

export async function GET() {
  if ((await sessionToken()) === null) return NextResponse.json(SIGNED_OUT);

  const { principal, role } = await identity();
  const session: ConsoleSession = {
    signed_in: true,
    authority: role === null ? "unverified" : "verified",
    principal,
    role,
  };
  return NextResponse.json(session);
}

export async function POST(request: Request) {
  let token: unknown;
  try {
    token = (await request.json())?.token;
  } catch {
    token = undefined;
  }

  if (typeof token !== "string" || token.trim().length === 0) {
    return NextResponse.json({ message: "A bearer token is required." }, { status: 400 });
  }

  const probe = await callControlPlane<ExceptionSummary[]>("/api/v1/exceptions", {
    query: { limit: "1" },
    token: token.trim(),
  });
  if (!probe.ok) return NextResponse.json(probe.failure, { status: probe.failure.status || 502 });

  const jar = await cookies();
  jar.set(SESSION_COOKIE, token.trim(), {
    httpOnly: true,
    sameSite: "strict",
    secure: cookieSecure(),
    path: "/",
  });

  const { principal, role } = await identity(token.trim());
  const session: ConsoleSession = {
    signed_in: true,
    authority: role === null ? "unverified" : "verified",
    principal,
    role,
  };
  return NextResponse.json(session);
}

export async function DELETE() {
  const jar = await cookies();
  jar.delete(SESSION_COOKIE);
  return NextResponse.json(SIGNED_OUT);
}
