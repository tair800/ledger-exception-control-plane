import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ConsoleSessionProvider } from "@/components/console-session";
import { DEMO_ROLES, demoSignInEnabled, demoToken } from "@/lib/server/demo-roles";
import { meta, mockControlPlane, session } from "@/test/fixtures";

/**
 * The public demonstration's one-click roles.
 *
 * What must hold: a visitor can sign in without knowing a token; only a *role name* leaves the
 * browser; the published token is validated server-side through the same path as a typed one; the
 * buttons exist only where the deployment switches them on; and a control plane that is waking
 * from free-tier sleep is waited out rather than reported as a failed sign-in.
 */

const SIGNED_OUT = {
  signed_in: false,
  authority: "unverified" as const,
  principal: null,
  role: null,
};

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the server-side role table", () => {
  it("is off unless the deployment enables it", () => {
    expect(demoSignInEnabled()).toBe(false);
    expect(demoToken("analyst")).toBeNull();
  });

  it("maps exactly the three published demonstration roles when enabled", () => {
    vi.stubEnv("CONSOLE_DEMO_SIGN_IN", "true");
    expect([...DEMO_ROLES]).toEqual(["analyst", "operator", "controller"]);
    expect(demoToken("analyst")).toBe("demo-analyst");
    expect(demoToken("operator")).toBe("demo-operator");
    expect(demoToken("controller")).toBe("demo-controller");
    expect(demoToken("admin")).toBeNull();
    expect(demoToken(undefined)).toBeNull();
  });
});

describe("the one-click route", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("validates the role's token through the same session path as a typed token", async () => {
    vi.stubEnv("CONSOLE_DEMO_SIGN_IN", "true");
    const establishSession = vi.fn(async () => Response.json(session("operator")));
    vi.doMock("@/lib/server/session", () => ({ establishSession }));
    const route = await import("@/app/api/console/session/demo/route");

    const response = await route.POST(
      new Request("http://console.test/api/console/session/demo", {
        method: "POST",
        body: JSON.stringify({ role: "operator" }),
      }),
    );

    expect(response.status).toBe(200);
    expect(establishSession).toHaveBeenCalledWith("demo-operator");
  });

  it("refuses when demo sign-in is off, and refuses an unknown role", async () => {
    const establishSession = vi.fn();
    vi.doMock("@/lib/server/session", () => ({ establishSession }));
    const route = await import("@/app/api/console/session/demo/route");

    const off = await route.POST(
      new Request("http://console.test/x", { method: "POST", body: JSON.stringify({ role: "analyst" }) }),
    );
    expect(off.status).toBe(404);
    expect(await (await route.GET()).json()).toEqual({ enabled: false, roles: [] });

    vi.stubEnv("CONSOLE_DEMO_SIGN_IN", "true");
    const unknown = await route.POST(
      new Request("http://console.test/x", { method: "POST", body: JSON.stringify({ role: "root" }) }),
    );
    expect(unknown.status).toBe(400);
    expect(establishSession).not.toHaveBeenCalled();
  });
});

describe("the sign-in screen", () => {
  it("offers three labelled demo roles and signs in with a role name, never a token", async () => {
    const { recorded } = mockControlPlane({
      "GET /api/console/session": { body: SIGNED_OUT },
      "GET /api/console/session/demo": {
        body: { enabled: true, roles: ["analyst", "operator", "controller"] },
      },
      "POST /api/console/session/demo": { body: session("operator") },
      "GET /api/console/meta": { body: meta() },
    });

    render(
      <ConsoleSessionProvider>
        <p>console content</p>
      </ConsoleSessionProvider>,
    );

    expect(await screen.findByText("Public synthetic demo roles")).toBeInTheDocument();
    for (const label of ["Analyst", "Operator", "Controller"]) {
      expect(screen.getByRole("button", { name: new RegExp(`Explore as ${label}`) })).toBeInTheDocument();
    }
    expect(screen.getByText(/not real accounts/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Explore as Operator/ }));

    expect(await screen.findByText("console content")).toBeInTheDocument();
    const signIn = recorded.find((call) => call.method === "POST");
    expect(signIn).toEqual({
      url: "/api/console/session/demo",
      method: "POST",
      body: { role: "operator" },
    });
    // No request from the browser carried a token.
    expect(JSON.stringify(recorded)).not.toMatch(/demo-(analyst|operator|controller)/);
  });

  it("shows only the token form where demo sign-in is not offered", async () => {
    mockControlPlane({
      "GET /api/console/session": { body: SIGNED_OUT },
      "GET /api/console/session/demo": { body: { enabled: false, roles: [] } },
    });

    render(
      <ConsoleSessionProvider>
        <p>console content</p>
      </ConsoleSessionProvider>,
    );

    expect(await screen.findByLabelText("Control-plane bearer token")).toBeVisible();
    expect(screen.queryByTestId("demo-roles")).toBeNull();
  });

  it("waits out a control plane that is waking, then signs in, with no failure shown", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let attempts = 0;
    mockControlPlane({
      "GET /api/console/session": { body: SIGNED_OUT },
      "GET /api/console/session/demo": { body: { enabled: true, roles: ["analyst"] } },
      "POST /api/console/session/demo": () => {
        attempts += 1;
        // Render's edge answered 429 during a real cold start, and 503 while a container starts.
        if (attempts === 1) {
          return { status: 429, body: { status: 429, message: "The control plane refused the request (429).", authority: false, not_implemented: false } };
        }
        return attempts === 2
          ? { status: 503, body: { status: 503, message: "The control plane failed while handling the request.", authority: false, not_implemented: false } }
          : { body: session("analyst") };
      },
      "GET /api/console/meta": { body: meta() },
    });

    render(
      <ConsoleSessionProvider>
        <p>console content</p>
      </ConsoleSessionProvider>,
    );

    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(await screen.findByRole("button", { name: /Explore as Analyst/ }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_500);
    });
    expect(screen.getByTestId("waking-notice")).toHaveTextContent("Starting the demo backend");
    expect(screen.queryByRole("alert")).toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(8_000);
    });
    expect(await screen.findByText("console content")).toBeInTheDocument();
    expect(attempts).toBe(3);
  });
});
