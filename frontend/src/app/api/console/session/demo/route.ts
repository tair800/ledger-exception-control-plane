/**
 * One-click sign-in for the public demonstration's three roles.
 *
 * `GET` says whether this console offers them. `POST {role}` signs in as that role: the published
 * demonstration token for it is looked up here, on the server, and goes through `establishSession`
 * — the same validation against the control plane, the same httpOnly cookie — as a typed token.
 * See `lib/server/demo-roles.ts` for why this is safe and when it is switched on.
 */

import { NextResponse } from "next/server";

import { DEMO_ROLES, demoSignInEnabled, demoToken } from "@/lib/server/demo-roles";
import { establishSession } from "@/lib/server/session";
import type { DemoSignIn } from "@/lib/types";

// The first sign-in after an idle period waits for the free-tier control plane to start.
export const maxDuration = 60;

export async function GET() {
  const offer: DemoSignIn = demoSignInEnabled()
    ? { enabled: true, roles: [...DEMO_ROLES] }
    : { enabled: false, roles: [] };
  return NextResponse.json(offer);
}

export async function POST(request: Request) {
  if (!demoSignInEnabled()) {
    return NextResponse.json(
      { message: "This console does not offer demonstration roles." },
      { status: 404 },
    );
  }

  let role: unknown;
  try {
    role = (await request.json())?.role;
  } catch {
    role = undefined;
  }

  const token = demoToken(role);
  if (token === null) {
    return NextResponse.json(
      { message: `Choose one of the demonstration roles: ${DEMO_ROLES.join(", ")}.` },
      { status: 400 },
    );
  }

  return establishSession(token);
}
