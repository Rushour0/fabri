// Thin wrappers over the `fabri serve` HTTP API (src/fabri/service/http_server.py).
// All paths are same-origin — the Vite dev server proxies them to `fabri serve`.
import type { CostSurface, RunResult } from "./events";

const unauthorizedListeners = new Set<() => void>();

export function onUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener);
  return () => unauthorizedListeners.delete(listener);
}

async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const res = await fetch(input, { ...init, credentials: "same-origin" });
  if (res.status === 401) unauthorizedListeners.forEach((listener) => listener());
  return res;
}

export interface SubmitResponse {
  session_id: string;
  status: string;
}

export interface SubmitOptions {
  threadId?: string;
  fleetId?: string;
  overrides?: Record<string, unknown>;
  // In catalog mode, which roster entry (agency/company name) to run this task on.
  catalogRef?: string;
}

export async function submitRun(task: string, opts: SubmitOptions = {}): Promise<SubmitResponse> {
  const body: Record<string, unknown> = { task };
  if (opts.overrides) body.overrides = opts.overrides;
  if (opts.threadId) body.thread_id = opts.threadId;
  if (opts.fleetId) body.fleet_id = opts.fleetId;
  if (opts.catalogRef) body.catalog_ref = opts.catalogRef;
  const res = await apiFetch("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.error ?? `submit failed (${res.status})`);
  }
  return res.json();
}

// Answer a mid-run `ask_user` question. The endpoint is added by the HITL
// backend slice; correlation is by `question_id`.
export async function answerAsk(
  sessionId: string,
  questionId: string,
  answer: string,
  selectedOption?: string,
): Promise<void> {
  const res = await apiFetch(`/runs/${encodeURIComponent(sessionId)}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question_id: questionId,
      answer,
      ...(selectedOption !== undefined ? { selected_option: selectedOption } : {}),
    }),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.error ?? `answer failed (${res.status})`);
  }
}

// Terminate a still-running run (POST /runs/<id>/cancel).
export async function cancelRun(sessionId: string): Promise<void> {
  const res = await apiFetch(`/runs/${encodeURIComponent(sessionId)}/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.error ?? `cancel failed (${res.status})`);
  }
}

// A run summary from the persisted session index (GET /runs).
export interface SessionSummary {
  session_id: string;
  task?: string;
  label?: string | null;
  status: "running" | "done" | "error" | "cancelled";
  outcome?: string;
  thread_id?: string | null;
  fleet_id?: string | null;
  started_ts?: number;
  ended_ts?: number;
  cost?: CostSurface | null;
}

export async function listRuns(): Promise<SessionSummary[]> {
  const res = await apiFetch("/runs");
  if (!res.ok) throw new Error(`list runs failed (${res.status})`);
  const body = await res.json();
  return body.sessions ?? [];
}

// Blocking result fetch (GET /runs/<id>/result) — used to replay a finished run
// selected from history.
export async function getResult(sessionId: string): Promise<RunResult> {
  const res = await apiFetch(`/runs/${encodeURIComponent(sessionId)}/result`);
  if (!res.ok) throw new Error(`result failed (${res.status})`);
  return res.json();
}

// ---- questions inbox (GET /questions) ----

// One ask_user question still awaiting a human answer, across every live run.
export interface PendingQuestion {
  question_id: string;
  question: string;
  options?: string[];
  default?: string;
  asked_ts: number; // epoch seconds when the agent asked
  session_id: string; // the run to answer against
  task?: string; // the source run's task line (its agency label)
  label?: string; // fleet-item label, when the run belongs to a fleet
}

// Every pending ask_user question across all in-flight runs, oldest first.
export async function listQuestions(): Promise<PendingQuestion[]> {
  const res = await apiFetch("/questions");
  if (!res.ok) throw new Error(`list questions failed (${res.status})`);
  const body = await res.json();
  return body.questions ?? [];
}

// ---- company org (GET /company) ----

// One node in a served company's org chart. `kind: "manager"` is a generated
// orchestrator; `"crew"` is a leaf agency. `id` equals the compiled agent name,
// so a live run's handoffs can be matched to nodes by id.
export interface CompanyNode {
  id: string;
  title: string;
  kind: "manager" | "crew";
  report_to: string;
  agency: string | null; // the source agency name, for crew (leaf) nodes
  children: string[];
}

export interface Company {
  name: string;
  title: string;
  positioning: string;
  max_cost_usd: number | null;
  root_id: string;
  nodes: CompanyNode[];
}

// The served company's org structure, or null when Studio is pointed at a single
// agency (`--config`) rather than a company (`--company`).
export async function getCompany(): Promise<Company | null> {
  const res = await apiFetch("/company");
  if (!res.ok) throw new Error(`get company failed (${res.status})`);
  const body = await res.json();
  return body.company ?? null;
}

// ---- catalog (GET /catalog) — the roster Studio serves in `--catalog` mode ----

export interface CatalogAgency {
  ref: string;
  kind: "agency";
  name: string;
  title: string;
  tagline?: string;
  category?: string;
  deliverable?: string;
  agents?: number;
  tools?: number;
  max_cost_usd?: number;
  self_improving?: boolean;
}

export interface CatalogCompany {
  ref: string;
  kind: "company";
  name: string;
  title: string;
  positioning?: string;
  node_count?: number;
  member_agencies?: string[];
  max_cost_usd?: number;
  org: Company; // the compiled org structure, for the Company org-chart
}

export interface Catalog {
  agencies: CatalogAgency[];
  companies: CatalogCompany[];
}

// The served roster, or null when Studio runs a single agency/company (not
// `--catalog` mode). Non-null → Studio shows the catalog as its front door.
export async function getCatalog(): Promise<Catalog | null> {
  const res = await apiFetch("/catalog");
  if (!res.ok) throw new Error(`get catalog failed (${res.status})`);
  const body = await res.json();
  return body.catalog ?? null;
}

export interface SlackInstall {
  team_id: string;
  team_name: string | null;
  installed_at: number | null;
}

export async function listSlackInstalls(): Promise<SlackInstall[]> {
  const res = await apiFetch("/slack/installs");
  if (!res.ok) throw new Error(`list slack installs failed (${res.status})`);
  return (await res.json()).installs ?? [];
}

export async function disconnectSlack(teamId: string): Promise<void> {
  const res = await apiFetch(`/slack/installs/${encodeURIComponent(teamId)}/delete`, { method: "POST" });
  if (!res.ok) throw new Error(`disconnect slack failed (${res.status})`);
}

// ---- fleets (POST/GET /fleets) ----

export interface FleetItem {
  task: string;
  label?: string;
  overrides?: Record<string, unknown>;
}

export interface FleetTotals {
  total_cost_usd: number;
  cost_by_model: Record<string, number>;
}

export interface FleetSummary {
  fleet_id: string;
  size: number;
  counts: Record<string, number>;
  totals: FleetTotals;
  started_ts?: number;
}

export interface FleetStatus {
  fleet_id: string;
  sessions: SessionSummary[];
  counts: Record<string, number>;
  totals: FleetTotals;
}

export async function submitFleet(
  items: FleetItem[],
  overrides?: Record<string, unknown>,
): Promise<{ fleet_id: string; sessions: { session_id: string; label?: string }[] }> {
  const res = await apiFetch("/fleets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items, ...(overrides ? { overrides } : {}) }),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.error ?? `fleet launch failed (${res.status})`);
  }
  return res.json();
}

export async function listFleets(): Promise<FleetSummary[]> {
  const res = await apiFetch("/fleets");
  if (!res.ok) throw new Error(`list fleets failed (${res.status})`);
  return (await res.json()).fleets ?? [];
}

export async function getFleetStatus(fleetId: string): Promise<FleetStatus> {
  const res = await apiFetch(`/fleets/${encodeURIComponent(fleetId)}`);
  if (!res.ok) throw new Error(`fleet status failed (${res.status})`);
  return res.json();
}

export type { RunResult };
