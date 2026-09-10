/**
 * The control-plane response shapes the console reads.
 *
 * Hand-written rather than generated, and checked against the committed `openapi.json` by
 * `src/test/contract.test.ts`: every model below publishes its key manifest, and the test fails if
 * the schema gains, loses or renames a field. A generator would have produced the same types with
 * no such assertion, and drift in a financial console is exactly the failure worth catching.
 *
 * **Every monetary value is a `string` and stays one.** The API returns the amount the deterministic
 * calculator produced, already quantised, with its currency beside it. Widening it to `number` here
 * would be the first step of re-deriving money in a browser, so the type refuses to allow it.
 */

// ---------------------------------------------------------------------------------------------
// Closed vocabularies. Mirrors of the backend enums, kept as unions so an unexpected value is a
// type error at the boundary rather than a silently rendered surprise.
// ---------------------------------------------------------------------------------------------

/** The only channel from the model into the money path (`PROJECT_SPEC.md` §6.1). */
export const TREATMENT_CODES = ["rebook", "accrue", "write_off", "escalate"] as const;
export type TreatmentCode = (typeof TREATMENT_CODES)[number];

/**
 * Confidence as a closed band, never a number.
 *
 * The model does not emit a probability and the console must not invent one: there is no percentage
 * to render, no bar to fill proportionally, and no threshold arithmetic to do.
 */
export const CONFIDENCE_BANDS = ["low", "medium", "high"] as const;
export type ConfidenceBand = (typeof CONFIDENCE_BANDS)[number];

/** The three roles §16 separates. Authority is decided server-side; this is for rendering only. */
export const ROLES = ["analyst", "controller", "operator"] as const;
export type Role = (typeof ROLES)[number];

export const RECOVERY_RESOLUTIONS = [
  "confirmed_by_evidence",
  "rejected_by_evidence",
  "resolved_unverified",
] as const;
export type RecoveryResolution = (typeof RECOVERY_RESOLUTIONS)[number];

/** The verbs the decision endpoints expose. `edit` is a separate route, not a flag on approve. */
export const DECISION_VERBS = ["approve", "edit", "reject"] as const;
export type DecisionVerb = (typeof DECISION_VERBS)[number];

// ---------------------------------------------------------------------------------------------
// Response models
// ---------------------------------------------------------------------------------------------

export interface ExceptionSummary {
  id: string;
  classification: string;
  status: string;
  psp_reference: string | null;
  currency: string | null;
  /** The settlement line's amount, as text. Never parsed, never totalled. */
  amount: string | null;
  correlation_id: string;
  has_proposal: boolean;
  decided: boolean;
}

export const EXCEPTION_SUMMARY_KEYS = [
  "id",
  "classification",
  "status",
  "psp_reference",
  "currency",
  "amount",
  "correlation_id",
  "has_proposal",
  "decided",
] as const;

export interface EvidenceView {
  id: string;
  kind: string;
  content: string;
  /** Whether the proposal actually cited this record — the distinction a reviewer needs. */
  cited: boolean;
}

export const EVIDENCE_VIEW_KEYS = ["id", "kind", "content", "cited"] as const;

export interface PostingAttemptView {
  attempt_no: number;
  state: string;
  outcome: string | null;
  sent_at: string;
  posting_ref: string | null;
}

export const POSTING_ATTEMPT_VIEW_KEYS = [
  "attempt_no",
  "state",
  "outcome",
  "sent_at",
  "posting_ref",
] as const;

export interface AuditEventView {
  id: string;
  occurred_at: string;
  principal: string;
  /** Null because this system is not an agent. Rendered as a named gap, not as blank space. */
  agent_identity: string | null;
  tool: string;
  scope_granted: string;
  approval_decision: string;
  approver: string | null;
  model: string | null;
  /** Null while no model call is made. Same rule as `agent_identity`. */
  region_jurisdiction: string | null;
  outcome: string;
  correlation_id: string;
}

export const AUDIT_EVENT_VIEW_KEYS = [
  "id",
  "occurred_at",
  "principal",
  "agent_identity",
  "tool",
  "scope_granted",
  "approval_decision",
  "approver",
  "model",
  "region_jurisdiction",
  "outcome",
  "correlation_id",
] as const;

/**
 * The settlement line the exception was raised on.
 *
 * Typed loosely because the API returns it as an open string map. The keys below are the ones
 * `routes.py::exception_detail` writes; a missing one renders as an explicit gap.
 */
export interface SettlementLineView {
  psp_reference?: string | null;
  merchant_reference?: string | null;
  transaction_type?: string | null;
  amount?: string | null;
  currency?: string | null;
  value_date?: string | null;
}

/**
 * What the model proposed, or that it abstained.
 *
 * `rationale` is **provenance for humans only**. It is displayed, labelled as model-generated, and
 * never read by code: nothing here is parsed, no number is extracted from it, and no branch is
 * taken on its content.
 */
export interface TreatmentProposalView {
  id?: string | null;
  treatment?: string | null;
  confidence?: string | null;
  rationale?: string | null;
  /** `true` when the model declined to propose. A distinct state, not a missing value. */
  abstained?: boolean | null;
  model_id?: string | null;
  model_version?: string | null;
}

export interface ApprovalView {
  id?: string | null;
  decision?: string | null;
  approved_treatment?: string | null;
  principal?: string | null;
  requested_by?: string | null;
  decided_at?: string | null;
}

/**
 * The deterministic adjustment: the only place a posted amount comes from.
 *
 * `amount` is rendered verbatim. `operation_id` is the retry-independent identifier that binds the
 * instruction payload and makes a re-send recognisable to the ledger.
 */
export interface AdjustmentView {
  id?: string | null;
  amount?: string | null;
  currency?: string | null;
  account_code?: string | null;
  period?: string | null;
  operation_id?: string | null;
  posting_ref?: string | null;
}

export interface OutboxView {
  state?: string | null;
  last_outcome?: string | null;
  attempt_count?: number | null;
}

export interface ExceptionDetail {
  id: string;
  classification: string;
  status: string;
  correlation_id: string;
  line: SettlementLineView;
  evidence: EvidenceView[];
  proposal: TreatmentProposalView | null;
  approval: ApprovalView | null;
  adjustment: AdjustmentView | null;
  outbox: OutboxView | null;
  attempts: PostingAttemptView[];
  audit: AuditEventView[];
}

export const EXCEPTION_DETAIL_KEYS = [
  "id",
  "classification",
  "status",
  "correlation_id",
  "line",
  "evidence",
  "proposal",
  "approval",
  "adjustment",
  "outbox",
  "attempts",
  "audit",
] as const;

export interface DecisionResponse {
  approval_id: string;
  exception_id: string;
  resolution_version: number;
  decision: string;
  approved_treatment: string | null;
  principal: string;
  /** §10: the decision endpoints return the claimed idempotency key. */
  approval_token: string;
}

export const DECISION_RESPONSE_KEYS = [
  "approval_id",
  "exception_id",
  "resolution_version",
  "decision",
  "approved_treatment",
  "principal",
  "approval_token",
] as const;

export interface DecisionRequest {
  resolution_version: number;
  approval_token: string;
  treatment?: TreatmentCode | null;
  treatment_proposal_id?: string | null;
  requested_by?: string | null;
}

export const DECISION_REQUEST_KEYS = [
  "resolution_version",
  "approval_token",
  "treatment",
  "treatment_proposal_id",
  "requested_by",
] as const;

export interface DeadLetterView {
  id: string;
  outbox_id: string;
  adjustment_id: string;
  operation_id: string;
  reason: string;
  attempts: number;
  replay_state: string;
  created_at: string;
  replayed_at: string | null;
}

/**
 * What a replay did, in the control plane's own terms.
 *
 * Every field exists on the server's `ReplayReportView`. Note what is *not* here: no applied count.
 * The server's own note records that an earlier version invented one, and a response type that
 * invents a field is how a console displays a number the system never measured.
 */
export interface ReplayReportView {
  dlq_id: string;
  adjustment_id: string;
  operation_id: string;
  outcome: string;
  posting_ref: string | null;
  detail: string;
  resolved: boolean;
}

/**
 * What an injected fault did to the books, and what the system concluded.
 *
 * The two are separate fields because their difference *is* the demonstration:
 * `recorded_outcome` is what the system was able to conclude (`unknown` — it refused to guess),
 * and `ledger_applied_count` is the simulated ledger's own count for that operation identifier.
 * Rendering only one of them would show a visitor the wrong thing.
 */
/**
 * One exception the demo-mode fault injector would accept.
 *
 * Carries no monetary amount. Choosing which posting to fault does not need one, and a select
 * option is not a place to start rendering money.
 */
/** What a reset of the public demonstration left behind. */
export interface DemoResetReport {
  exceptions: number;
  approved: number;
  dispatched: number;
  dead_lettered: number;
  in_recovery: number;
  awaiting_dispatch: number;
  explanation: string;
}

export interface FaultTargetView {
  exception_id: string;
  psp_reference: string | null;
  classification: string;
  operation_id: string;
}

export interface InjectedFaultReport {
  adjustment_id: string;
  operation_id: string;
  fault: string;
  recorded_outcome: string;
  ledger_applied_count: number;
  ledger_posts_received: number;
  explanation: string;
}

/** The authenticated principal, with the authority the *server* will enforce. */
export interface IdentityView {
  principal: string;
  role: string;
  may_record_decision: boolean;
  may_authorise: boolean;
  may_edit_treatment: boolean;
  may_work_operations_queues: boolean;
}

export const DEAD_LETTER_VIEW_KEYS = [
  "id",
  "outbox_id",
  "adjustment_id",
  "operation_id",
  "reason",
  "attempts",
  "replay_state",
  "created_at",
  "replayed_at",
] as const;

export interface RecoveryItemView {
  id: string;
  adjustment_id: string;
  operation_id: string;
  reason: string;
  /** Returned in full: the operator is told what to inspect, not given a code to look up. */
  evidence_procedure: string;
  opened_at: string;
  sla_due_at: string;
  approving_principal: string;
  overdue: boolean;
}

export const RECOVERY_ITEM_VIEW_KEYS = [
  "id",
  "adjustment_id",
  "operation_id",
  "reason",
  "evidence_procedure",
  "opened_at",
  "sla_due_at",
  "approving_principal",
  "overdue",
] as const;

export interface ResolveRequest {
  resolution: RecoveryResolution;
  posting_ref?: string | null;
}

export const RESOLVE_REQUEST_KEYS = ["resolution", "posting_ref"] as const;

// ---------------------------------------------------------------------------------------------
// Console-local shapes (served by this app's own route handlers, not by the control plane)
// ---------------------------------------------------------------------------------------------

/**
 * Who the console believes it is acting as.
 *
 * `authority: "verified"` means the control plane told us the principal and role. `"unverified"`
 * means the identity endpoint is not available on the connected control plane, so the console
 * cannot know the role and must not claim one — see `Session` handling in `src/lib/session.ts`.
 */
export interface ConsoleSession {
  signed_in: boolean;
  authority: "verified" | "unverified";
  principal: string | null;
  role: Role | null;
}

/**
 * What the console knows about the connected instance.
 *
 * `demo_mode: "unknown"` is a real state, not a placeholder: no endpoint reports it yet, and a
 * console that guessed `false` would disable the fault-injection control for the wrong reason —
 * "this instance is not a demo" rather than "nobody has told me". The two look identical to a
 * visitor and only one of them is true.
 */
export interface ConsoleMeta {
  demo_mode: boolean | "unknown";
  version: string | null;
  /** Whether the control plane was reachable at all when this was read. */
  reachable: boolean;
  /** Which optional endpoints the connected control plane publishes. */
  capabilities: {
    dlq_replay: boolean;
    demo_inject_crash: boolean;
    demo_fault_targets: boolean;
    demo_reset: boolean;
    identity: boolean;
    meta: boolean;
    request_edit: boolean;
  };
}

/** A refusal the console can explain, as opposed to a transport failure it can only report. */
export interface ApiFailure {
  status: number;
  /** A short, human-readable line. Never a raw stack trace or upstream body. */
  message: string;
  /** The backend refusal reason code, where it sent one (`{"reason": ...}`). */
  reason?: string;
  /** True for 401/403: the console renders these as authority messages, not as errors. */
  authority: boolean;
  /** True when the endpoint does not exist on the connected control plane yet. */
  not_implemented: boolean;
}
