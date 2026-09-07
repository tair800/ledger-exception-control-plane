import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ConsoleSessionProvider } from "@/components/console-session";
import { DeadLetterQueue } from "@/components/dlq";
import { DemoControl } from "@/components/demo";
import { Queue } from "@/components/queue";
import { RecoveryQueue } from "@/components/recovery";
import {
  deadLetter,
  mockControlPlane,
  mockPendingAfterSession,
  sessionRoutes,
  summary,
} from "@/test/fixtures";

/**
 * Loading, empty and failure — asserted on every screen that fetches.
 *
 * The three are not interchangeable and the distinctions are the point. An empty dead-letter queue
 * means nothing failed. A refused one means you are not allowed to know. A console that rendered
 * both as "no results" would be quietly telling an analyst that no dispatch had failed, which is a
 * statement it has no basis for.
 */

function withSession(node: ReactNode) {
  return render(<ConsoleSessionProvider>{node}</ConsoleSessionProvider>);
}

describe("loading", () => {
  it("the console reports that it is opening before the session is known", () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => new Promise<Response>(() => {})),
    );

    render(
      <ConsoleSessionProvider>
        <Queue />
      </ConsoleSessionProvider>,
    );

    expect(screen.getByRole("status")).toHaveTextContent("Opening the console…");
  });

  it("the queue reports that it is loading before rows arrive", async () => {
    mockPendingAfterSession("controller");

    withSession(<Queue />);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Loading the exception queue…"),
    );
  });

  it("the recovery queue reports that it is loading", async () => {
    mockPendingAfterSession("operator");

    withSession(<RecoveryQueue />);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Loading the recovery queue…"),
    );
  });

  it("the dead-letter queue reports that it is loading", async () => {
    mockPendingAfterSession("operator");

    withSession(<DeadLetterQueue />);

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Loading the dead-letter queue…"),
    );
  });
});

describe("empty", () => {
  it("an empty queue says nothing has been raised, and how to raise something", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      "GET /api/console/exceptions?limit=200": { body: [] },
    });

    withSession(<Queue />);

    expect(await screen.findByText("No exceptions")).toBeInTheDocument();
    expect(screen.getByText(/Ingest a settlement file/)).toBeInTheDocument();
  });

  it("a filtered-out queue is distinguished from an empty one", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      "GET /api/console/exceptions?limit=200": { body: [summary()] },
    });

    withSession(<Queue />);
    await screen.findByRole("link", { name: "TEST-PSP-REF-0001" });

    // A substring that matches nothing.
    await userEvent.type(
      screen.getByLabelText("Reference or correlation id"),
      "NO-SUCH-REFERENCE",
    );

    expect(await screen.findByText("No exception matches these filters")).toBeInTheDocument();
    expect(screen.queryByText("No exceptions")).toBeNull();
  });

  it("an empty dead-letter queue says nothing is waiting, not that nothing failed", async () => {
    mockControlPlane({
      ...sessionRoutes("operator"),
      "GET /api/console/dlq?pending_only=true": { body: [] },
    });

    withSession(<DeadLetterQueue />);

    expect(await screen.findByText("No pending dead letters")).toBeInTheDocument();
    expect(screen.getByText(/Clear the filter/)).toBeInTheDocument();
  });

  it("an empty recovery queue explains what an empty one means", async () => {
    mockControlPlane({
      ...sessionRoutes("operator"),
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<RecoveryQueue />);

    expect(await screen.findByText("No open recovery items")).toBeInTheDocument();
  });
});

describe("failure", () => {
  it("a server failure is reported as a failure, with a retry", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      "GET /api/console/exceptions?limit=200": {
        status: 500,
        body: {
          status: 500,
          message: "The control plane failed while handling the request.",
          authority: false,
          not_implemented: false,
        },
      },
    });

    withSession(<Queue />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Request failed");
    expect(alert).toHaveTextContent("The control plane failed while handling the request.");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("an unreachable control plane is reported as unreachable, not as empty", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      "GET /api/console/exceptions?limit=200": {
        status: 502,
        body: {
          status: 0,
          message:
            "The control plane could not be reached (TimeoutError). Check that it is running and that CONTROL_PLANE_BASE_URL is correct.",
          authority: false,
          not_implemented: false,
        },
      },
    });

    withSession(<Queue />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("could not be reached");
    expect(screen.queryByText("No exceptions")).toBeNull();
  });

  it("a refusal is an authority message, and offers no retry", async () => {
    mockControlPlane({
      ...sessionRoutes("analyst"),
      "GET /api/console/dlq?pending_only=true": {
        status: 403,
        body: {
          status: 403,
          message: "The dead-letter queue is worked by the operator role.",
          reason: "role_may_not_recover",
          authority: true,
          not_implemented: false,
        },
      },
    });

    withSession(<DeadLetterQueue />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Insufficient authority");
    expect(alert).toHaveTextContent("worked by the operator role");
    expect(alert).toHaveTextContent("role_may_not_recover");

    // Retrying a refusal is pointless, so the console does not invite it.
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();

    // And it must never look like an empty queue.
    expect(screen.queryByText("No pending dead letters")).toBeNull();
    expect(screen.queryByText("No dead letters")).toBeNull();
  });
});

describe("controls whose endpoint does not exist", () => {
  it("replay is disabled, and says why, when the control plane does not publish it", async () => {
    mockControlPlane({
      ...sessionRoutes("operator"),
      "GET /api/console/dlq?pending_only=true": { body: [deadLetter()] },
    });

    withSession(<DeadLetterQueue />);

    const replay = await screen.findByRole("button", { name: "Replay" });
    expect(replay).toBeDisabled();
    expect(screen.getByText(/Replay is not available on this control plane\./)).toBeInTheDocument();
  });

  it("replay enables itself when the control plane publishes the endpoint", async () => {
    mockControlPlane({
      ...sessionRoutes("operator", {
        capabilities: {
          dlq_replay: true,
          demo_inject_crash: false,
          identity: true,
          meta: false,
          request_edit: false,
        },
      }),
      "GET /api/console/dlq?pending_only=true": { body: [deadLetter()] },
    });

    withSession(<DeadLetterQueue />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Replay" })).toBeEnabled());
  });

  it("fault injection is disabled outside demo mode", async () => {
    mockControlPlane({
      ...sessionRoutes("operator", { demo_mode: false }),
      "GET /api/console/exceptions?limit=200": { body: [summary()] },
    });

    withSession(<DemoControl />);

    const inject = await screen.findByRole("button", { name: /Crash after the socket write/ });
    expect(inject).toBeDisabled();
    expect(screen.getByText("disabled")).toBeInTheDocument();
  });

  it("fault injection is disabled, and says so, when demo state is unknown", async () => {
    mockControlPlane({
      ...sessionRoutes("operator"),
      "GET /api/console/exceptions?limit=200": { body: [summary()] },
    });

    withSession(<DemoControl />);

    const inject = await screen.findByRole("button", { name: /Crash after the socket write/ });
    expect(inject).toBeDisabled();
    expect(screen.getByText("unknown")).toBeInTheDocument();
    expect(screen.getByText(/nobody told me/)).toBeInTheDocument();
  });

  it("fault injection stays disabled in demo mode while the endpoint is missing", async () => {
    mockControlPlane({
      ...sessionRoutes("operator", { demo_mode: true }),
      "GET /api/console/exceptions?limit=200": { body: [summary()] },
    });

    withSession(<DemoControl />);

    const inject = await screen.findByRole("button", { name: /Crash after the socket write/ });
    expect(inject).toBeDisabled();
    expect(screen.getByText("not implemented")).toBeInTheDocument();
  });
});
