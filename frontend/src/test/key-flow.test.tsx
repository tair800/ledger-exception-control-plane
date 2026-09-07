import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { ConsoleSessionProvider } from "@/components/console-session";
import { ExceptionDetailView } from "@/components/exception-detail";
import { Queue } from "@/components/queue";
import {
  FIXTURE_EXCEPTION_ID,
  FIXTURE_PROPOSAL_ID,
  decidedDetail,
  decision,
  detail,
  mockControlPlane,
  recoveryItem,
  sessionRoutes,
  summary,
} from "@/test/fixtures";

/**
 * The key flow: queue → detail → approve.
 *
 * Run against a mocked network at this app's own route boundary, so it exercises the browser
 * client, the screens and the decision payload without a control plane. The assertions that matter
 * are the ones about the payload: what a click on "Approve" actually sends is the difference
 * between authorising a treatment code and authorising something else.
 */

function withSession(node: ReactNode) {
  return render(<ConsoleSessionProvider>{node}</ConsoleSessionProvider>);
}

describe("queue → detail → approve", () => {
  it("lists an exception and links to its provenance", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      "GET /api/console/exceptions?limit=200": { body: [summary()] },
    });

    withSession(<Queue />);

    const link = await screen.findByRole("link", { name: "TEST-PSP-REF-0001" });
    expect(link).toHaveAttribute("href", `/exceptions/${FIXTURE_EXCEPTION_ID}`);

    // The queue renders the line amount as text, beside its currency, and never totals a column.
    expect(screen.getByText("11.11")).toBeInTheDocument();
    expect(screen.queryByText(/total/i)).toBeNull();
  });

  it("shows the evidence, the proposal and the boundary between them", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: { body: detail() },
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    expect(await screen.findByText("Evidence pack")).toBeInTheDocument();
    expect(screen.getByText("cited by the proposal")).toBeInTheDocument();
    expect(screen.getByText("not cited")).toBeInTheDocument();

    // The rationale is labelled as model output and nothing parses it.
    expect(
      screen.getByText(/Model-generated rationale — provenance for humans only/),
    ).toBeInTheDocument();

    // Confidence is a band. No percentage is rendered anywhere on the screen.
    expect(screen.getByText("medium")).toBeInTheDocument();
    expect(screen.queryByText(/%/)).toBeNull();

    // Nothing has been authorised, so there is no amount to show.
    expect(screen.getByText("Nothing computed")).toBeInTheDocument();
  });

  it("approves the proposed treatment and reports the consumed idempotency key", async () => {
    let decided = false;

    const { recorded } = mockControlPlane({
      ...sessionRoutes("controller"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: () => ({
        body: decided ? decidedDetail() : detail(),
      }),
      [`POST /api/console/exceptions/${FIXTURE_EXCEPTION_ID}/decision`]: () => {
        decided = true;
        return { body: decision() };
      },
      "GET /api/console/recovery?stale=false": { body: [recoveryItem()] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    const approve = await screen.findByRole("button", { name: "Approve rebook" });
    await userEvent.click(approve);

    await waitFor(() => expect(screen.getByText("Decision recorded")).toBeInTheDocument());

    const submitted = recorded.find((call) => call.method === "POST");
    expect(submitted).toBeDefined();

    const body = submitted?.body as Record<string, unknown>;
    expect(body.verb).toBe("approve");
    // The treatment authorised is the one the model proposed, sent explicitly: approving does not
    // mean "whatever was proposed", it means "this code".
    expect(body.treatment).toBe("rebook");
    expect(body.treatment_proposal_id).toBe(FIXTURE_PROPOSAL_ID);
    expect(body.resolution_version).toBe(1);
    // An idempotency key is claimed by the console and returned by the API.
    expect(typeof body.approval_token).toBe("string");
    expect((body.approval_token as string).length).toBeGreaterThanOrEqual(8);
    // The console never names the actor: the control plane resolves it from the bearer token.
    expect(body).not.toHaveProperty("principal");
    // And it never sends an amount.
    expect(Object.keys(body)).not.toContain("amount");

    expect(screen.getByText("TEST-APPROVAL-TOKEN-0001")).toBeInTheDocument();

    // The screen reloads and now shows the deterministic adjustment, the operation identifier and
    // the dispatch that followed.
    await waitFor(() => expect(screen.getByText("TEST-OPERATION-ID-0001")).toBeInTheDocument());
    expect(screen.getByText("TEST-ACCOUNT-0001")).toBeInTheDocument();
    // `confirmed` appears twice: the outbox's last outcome, and the attempt that produced it.
    expect(screen.getAllByText("confirmed").length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText("TEST PROCEDURE: query the ledger for the operation identifier.")).toBeInTheDocument();
  });

  it("offers no decision control once a decision is recorded", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: { body: decidedDetail() },
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    await waitFor(() => expect(screen.getByText("Human decision")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText("TEST-PRINCIPAL-controller")).toBeInTheDocument();
  });
});

describe("role separation on the decision panel", () => {
  it("an analyst is offered rejection but never approval", async () => {
    mockControlPlane({
      ...sessionRoutes("analyst"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: { body: detail() },
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    expect(await screen.findByRole("button", { name: "Reject" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Authorise edited treatment/ })).toBeNull();

    // The request-an-edit path has no endpoint, and the console says so rather than filing nothing.
    const request = screen.getByRole("button", { name: /Request edit — endpoint not implemented/ });
    expect(request).toBeDisabled();
  });

  it("an operator is offered no decision at all, with the reason", async () => {
    mockControlPlane({
      ...sessionRoutes("operator"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: { body: detail() },
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    await waitFor(() => expect(screen.getByText("Human decision")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reject" })).toBeNull();
    expect(screen.getByText(/Your role holds no approval right/)).toBeInTheDocument();
  });
});

describe("abstention", () => {
  it("is rendered as its own state, not as a missing proposal", async () => {
    mockControlPlane({
      ...sessionRoutes("controller"),
      [`GET /api/console/exceptions/${FIXTURE_EXCEPTION_ID}`]: {
        body: detail({
          proposal: {
            id: FIXTURE_PROPOSAL_ID,
            treatment: "escalate",
            confidence: "low",
            rationale: "TEST RATIONALE: the evidence does not settle the question.",
            abstained: true,
            model_id: "test-model",
            model_version: "test-version",
          },
        }),
      },
      "GET /api/console/recovery?stale=false": { body: [] },
    });

    withSession(<ExceptionDetailView exceptionId={FIXTURE_EXCEPTION_ID} />);

    expect(await screen.findByText("The model abstained")).toBeInTheDocument();
    expect(screen.queryByText("No proposal on record")).toBeNull();
    expect(screen.getByText("low")).toBeInTheDocument();
  });
});
