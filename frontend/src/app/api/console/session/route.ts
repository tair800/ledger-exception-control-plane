/**
 * The console session: a bearer token in an httpOnly cookie, and what the control plane says it is.
 *
 * `POST` validates the token against the control plane *before* setting the cookie, so a typo is a
 * sign-in error rather than a console that renders empty and blames the backend. Validation is a
 * read of the exception queue, which every configured role may perform. The validation itself lives
 * in `lib/server/session.ts`, shared with the public demo's one-click roles so both take one path.
 *
 * `GET` reports the session. Where the control plane publishes `GET /api/v1/me` the principal and
 * role come from it and authority is `"verified"`. Where it does not, authority is `"unverified"`
 * and the console says so on every screen: it will not name a role the server did not confirm,
 * because a role the client picked is the exact thing `routes.py` refuses to accept.
 */

import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { SESSION_COOKIE, sessionToken } from "@/lib/server/backend";
import { establishSession, identity } from "@/lib/server/session";
import type { ConsoleSession } from "@/lib/types";

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

  return establishSession(token.trim());
}

export async function DELETE() {
  const jar = await cookies();
  jar.delete(SESSION_COOKIE);
  return NextResponse.json(SIGNED_OUT);
}
