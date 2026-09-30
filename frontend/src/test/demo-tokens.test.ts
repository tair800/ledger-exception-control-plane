import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEMO_ROLES, demoToken, type DemoRole } from "@/lib/server/demo-roles";
import {
  FRONTEND_ROOT,
  applicationSources,
  findServerModuleImports,
  isRouteHandler,
  type Violation,
} from "@/test/source-scan";

/**
 * Where the public demonstration's tokens live, and where they go.
 *
 * The three published tokens are code in two committed places: the repository's `Makefile`, beside
 * the registry hashes that make them valid, and `src/lib/server/demo-roles.ts`, which presents them
 * for the one-click roles. The `Makefile` is the authority. This file holds the console's side:
 *
 * - `demo-roles.ts` is the only console source holding a token, and each of its tokens is the
 *   `Makefile`'s token for the same role;
 * - nothing but a route handler imports a server module, so the table is in no bundle a browser
 *   receives;
 * - a one-click sign-in answers with the session alone: the token goes to the control plane in the
 *   `Authorization` header and to the browser only as the httpOnly session cookie.
 *
 * The registry's own guard — its hashes appear in no tracked file but the `Makefile` — is in
 * `tests/test_config.py`, because it is a property of the whole repository rather than the console.
 */

const DEMO_ROLES_MODULE = join("src", "lib", "server", "demo-roles.ts");

/** The tokens, read from the table itself, so this file is not a third copy of them. */
function publishedTokens(): Record<DemoRole, string> {
  vi.stubEnv("CONSOLE_DEMO_SIGN_IN", "true");
  const tokens = Object.fromEntries(DEMO_ROLES.map((role) => [role, demoToken(role)]));
  for (const role of DEMO_ROLES) {
    expect(tokens[role], `the table has no token for ${role}`).toEqual(expect.any(String));
  }
  return tokens as Record<DemoRole, string>;
}

interface RegistryEntry {
  role: string;
  token_sha256: string;
}

/** The demonstration registry, as the `Makefile` loads it for `make demo-api`. */
function makefileRegistry(): RegistryEntry[] {
  const makefile = join(FRONTEND_ROOT, "..", "Makefile");
  expect(existsSync(makefile), "these tests expect the repository's Makefile beside frontend/").toBe(
    true,
  );

  const prefix = "DEMO_PRINCIPALS = ";
  const line = readFileSync(makefile, "utf8")
    .split(/\r?\n/)
    .find((text) => text.startsWith(prefix));
  expect(line, "the Makefile no longer defines DEMO_PRINCIPALS").toBeDefined();
  return Object.values(JSON.parse((line ?? "").slice(prefix.length)) as Record<string, RegistryEntry>);
}

/** What the control plane's registry stores for a token: its SHA-256, hex-encoded. */
function fingerprint(token: string): string {
  return createHash("sha256").update(token, "utf8").digest("hex");
}

function describeViolation(violation: Violation): string {
  return `${violation.path}:${violation.line} — ${violation.text}`;
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.doUnmock("next/headers");
});

describe("the published demo tokens", () => {
  it("are held by exactly one module the console ships, and it is a server module", () => {
    const tokens = Object.values(publishedTokens());
    const carriers = applicationSources()
      .filter((file) => tokens.some((token) => file.source.includes(token)))
      .map((file) => file.path);

    expect(carriers).toEqual([DEMO_ROLES_MODULE]);
  });

  it("are the Makefile's demonstration principals, role for role", () => {
    const tokens = publishedTokens();
    const registry = makefileRegistry();

    // Every principal the registry defines is offered, and no role is offered that it lacks.
    expect(registry.map((entry) => entry.role).sort()).toEqual([...DEMO_ROLES].sort());

    // A button signs in as the role it names: its token is that role's, and only that role's.
    for (const role of DEMO_ROLES) {
      const holders = registry.filter((entry) => entry.token_sha256 === fingerprint(tokens[role]));
      expect(holders.map((entry) => entry.role), `the ${role} button's token`).toEqual([role]);
    }
  });
});

describe("server modules", () => {
  it("are imported by route handlers and by each other, and by nothing else", () => {
    const sources = applicationSources();

    // The scan saw what it guards: route handlers, and the table the one-click route imports.
    expect(sources.some((file) => isRouteHandler(file.path))).toBe(true);
    expect(sources.map((file) => file.path)).toContain(DEMO_ROLES_MODULE);
    expect(findServerModuleImports(sources).map(describeViolation)).toEqual([]);
  });

  it("are caught however a page or component reaches for one", () => {
    const component = join("src", "components", "sign-in.tsx");
    const samples = [
      { path: component, source: 'import { demoToken } from "@/lib/server/demo-roles";' },
      { path: component, source: 'import { demoToken } from "../lib/server/demo-roles";' },
      { path: component, source: 'import {\n  demoToken,\n} from "@/lib/server/demo-roles";' },
      {
        path: join("src", "app", "page.tsx"),
        source: 'import type { Upstream } from "@/lib/server/backend";',
      },
      {
        path: join("src", "app", "demo", "page.tsx"),
        source: 'const table = await import("@/lib/server/demo-roles");',
      },
      {
        path: join("src", "lib", "client.ts"),
        source: 'export { demoToken } from "./server/demo-roles";',
      },
      { path: join("src", "lib", "client.ts"), source: 'import "./server/session";' },
    ];

    for (const sample of samples) {
      expect(findServerModuleImports([sample]), sample.source).toHaveLength(1);
    }
  });

  it("may be imported by a route handler or a server module, and are not confused with a neighbour", () => {
    const permitted = [
      {
        path: join("src", "app", "api", "console", "session", "demo", "route.ts"),
        source: 'import { demoToken } from "@/lib/server/demo-roles";',
      },
      {
        path: join("src", "lib", "server", "session.ts"),
        source: 'import { callControlPlane } from "@/lib/server/backend";',
      },
      {
        path: join("src", "components", "roles.tsx"),
        source: 'import { ROLES } from "@/lib/types";\nimport { clock } from "@/lib/server-time";',
      },
      {
        path: join("src", "components", "notes.tsx"),
        source: '// import { demoToken } from "@/lib/server/demo-roles";\nexport const note = "";',
      },
    ];

    expect(findServerModuleImports(permitted).map(describeViolation)).toEqual([]);
  });
});

describe("a one-click sign-in", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("answers with the session alone, and gives the token only to the control plane and the httpOnly cookie", async () => {
    const token = publishedTokens().operator;
    vi.stubEnv("CONTROL_PLANE_BASE_URL", "https://control-plane.test");
    vi.stubEnv("CONSOLE_COOKIE_SECURE", "true");

    const setCookie = vi.fn();
    vi.doMock("next/headers", () => ({
      cookies: async () => ({ get: () => undefined, set: setCookie }),
    }));

    // The control plane at the network boundary: what each request carried, and a real answer.
    const upstream: { path: string; authorization: string | null }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = new URL(String(input)).pathname;
        upstream.push({ path, authorization: new Headers(init?.headers).get("authorization") });
        if (path === "/openapi.json") return Response.json({ paths: { "/api/v1/me": {} } });
        if (path === "/api/v1/me") return Response.json({ principal: "operator-a", role: "operator" });
        return Response.json([]);
      }),
    );

    const { SESSION_COOKIE } = await import("@/lib/server/backend");
    const route = await import("@/app/api/console/session/demo/route");
    const response = await route.POST(
      new Request("http://console.test/api/console/session/demo", {
        method: "POST",
        body: JSON.stringify({ role: "operator" }),
      }),
    );
    const body = await response.text();

    // What the page can read: the session, and nothing else.
    expect(response.status).toBe(200);
    expect(JSON.parse(body)).toEqual({
      signed_in: true,
      authority: "verified",
      principal: "operator-a",
      role: "operator",
    });
    expect(body).not.toContain(token);

    // Where the token went: validated by the control plane, then kept in the httpOnly cookie.
    expect(upstream).toEqual([
      { path: "/api/v1/exceptions", authorization: `Bearer ${token}` },
      { path: "/openapi.json", authorization: null },
      { path: "/api/v1/me", authorization: `Bearer ${token}` },
    ]);
    expect(setCookie).toHaveBeenCalledTimes(1);
    expect(setCookie).toHaveBeenCalledWith(SESSION_COOKIE, token, {
      httpOnly: true,
      sameSite: "strict",
      secure: true,
      path: "/",
    });
  });
});
