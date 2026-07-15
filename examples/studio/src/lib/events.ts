// Client-side mirror of fabri's `EventType` (src/fabri/events.py). Kept in sync
// by convention — fabri's enum is the single source of truth. We only enumerate
// the events Studio renders; unknown `type`s are tolerated and shown as raw.

export const EventType = {
  START: "start",
  STEP_STARTED: "step_started",
  STEP_FINISHED: "step_finished",
  THOUGHT: "thought",
  TOOL_STARTED: "tool_started",
  TOOL_CALL: "tool_call",
  PARALLEL_GROUP_STARTED: "parallel_group_started",
  PLAN_STARTED: "plan_started",
  PLAN_ITEM_STARTED: "plan_item_started",
  PLAN_ITEM_FINISHED: "plan_item_finished",
  PLAN_FINISHED: "plan_finished",
  FINAL: "final",
  FAILED: "failed",
  INCOMPLETE: "incomplete",
  ERROR: "error",
  ASK_USER: "ask_user",
  USAGE: "usage",
  POST_RUN_USAGE: "post_run_usage",
  COST_UNACCOUNTED: "cost_unaccounted",
  DISCREPANCY: "discrepancy",
  NARRATION: "narration",
  RETRIEVAL: "retrieval",
  STRUCTURED_OUTPUT: "structured_output",
} as const;

// A raw fabri trace event. Every line carries `ts` + `type`; the rest is
// event-specific (see the field shapes in src/fabri/core/agent.py).
export interface FabriEvent {
  ts?: number;
  type: string;
  // Common optional fields across event types — all narrow by `type`.
  text?: string;
  outcome?: string;
  reason?: string;
  step?: number;
  name?: string; // tool_call / tool_started
  args?: Record<string, unknown>;
  result?: { ok?: boolean; error?: string; [k: string]: unknown };
  call_index?: number;
  parallel_group?: string;
  calls?: { call_index: number; name: string }[]; // parallel_group_started
  trigger?: string; // narration
  // plan_* (src/fabri/core/agent.py)
  items?: { goal?: string; artifacts?: string[]; [k: string]: unknown }[];
  order?: number[];
  index?: number;
  goal?: string;
  artifacts?: string[];
  ok?: boolean; // plan_item_finished
  items_completed?: number;
  items_total?: number;
  // discrepancy / cost_unaccounted
  path?: string;
  tool?: string;
  child_returncode?: number;
  child_stderr_tail?: string;
  // structured_output
  attempt?: number;
  valid?: boolean;
  errors?: string[];
  // ask_user (listener-injected — see the HITL backend slice)
  question_id?: string;
  question?: string;
  options?: string[];
  default?: string;
  // usage / cost
  cost_usd?: number;
  total_cost_usd?: number;
  subagent_cost_usd?: number;
  cost_by_model?: Record<string, number>;
  input_tokens?: number;
  output_tokens?: number;
  step_count?: number;
  wall_time_s?: number;
  subagent_count?: number;
  subagent_failed_count?: number;
  [k: string]: unknown;
}

// The cost + metrics surface returned by extract_cost (src/fabri/service/tailer.py):
// carried on the terminal SSE `result` frame and by GET /runs history.
export interface CostSurface {
  cost_usd?: number;
  subagent_cost_usd?: number;
  total_cost_usd?: number;
  post_run_cost_usd?: number;
  cost_by_model?: Record<string, number>;
  metrics?: {
    input_tokens?: number;
    output_tokens?: number;
    step_count?: number;
    wall_time_s?: number;
    subagent_count?: number;
    subagent_failed_count?: number;
    guideline_reuse_rate?: number | null;
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

// The result envelope carried by the terminal `event: result` SSE frame
// (FabriService.result() — src/fabri/service/service.py).
export interface RunResult {
  session_id: string;
  success?: boolean;
  outcome?: string;
  final_text?: string;
  structured_output?: unknown;
  usage?: Record<string, unknown>;
  cost?: CostSurface | null;
  error?: string | null;
}

// Studio's message taxonomy — how each fabri event class renders. The mapping
// from event → class is the core UI contract (see PM handoff).
export type MessageClass =
  | "manager" //   final              → the manager's definitive message (primary)
  | "narration" // narration          → light "what's happening now" line
  | "thought" //   thought            → collapsible main-model reasoning
  | "plan" //      plan_*             → an evolving step timeline (aggregated)
  | "tool" //      tool_started/call, spawn_subagent, parallel_group → tool-call card
  | "ask" //       ask_user           → interactive question card (primary)
  | "cost" //      usage              → cost / COGS summary
  | "warning" //   cost_unaccounted / discrepancy → a flagged risk to spend/truth
  | "note" //      structured_output  → a quiet validation/retry note
  | "terminal"; // failed/incomplete/error → run-ending status

const SPAWN_SUBAGENT = "spawn_subagent";

// Classify a raw event into a Studio message class, or null to drop it from the
// timeline (lifecycle bookkeeping the user doesn't need to see).
export function classify(ev: FabriEvent): MessageClass | null {
  switch (ev.type) {
    case EventType.FINAL:
      return "manager";
    case EventType.NARRATION:
      return "narration";
    case EventType.THOUGHT:
      return "thought";
    case EventType.PLAN_STARTED:
    case EventType.PLAN_ITEM_STARTED:
    case EventType.PLAN_ITEM_FINISHED:
    case EventType.PLAN_FINISHED:
      return "plan";
    case EventType.ASK_USER:
      return "ask";
    case EventType.USAGE:
      return "cost";
    case EventType.COST_UNACCOUNTED:
    case EventType.DISCREPANCY:
      return "warning";
    case EventType.STRUCTURED_OUTPUT:
      // Only the failed/retry attempts are worth a note; a first-try valid
      // parse is silent plumbing.
      return ev.valid === false ? "note" : null;
    case EventType.FAILED:
    case EventType.INCOMPLETE:
    case EventType.ERROR:
      return "terminal";
    case EventType.TOOL_STARTED:
    case EventType.TOOL_CALL:
      // ask_user surfaces as its own interactive card — don't also show a
      // generic tool card for it.
      return ev.name === "ask_user" ? null : "tool";
    case EventType.PARALLEL_GROUP_STARTED:
      return "tool";
    // Lifecycle / plumbing — not surfaced as messages.
    case EventType.START:
    case EventType.STEP_STARTED:
    case EventType.STEP_FINISHED:
    case EventType.RETRIEVAL:
    case EventType.POST_RUN_USAGE:
      return null;
    default:
      return null;
  }
}

// A short, human label for a tool/activity event. Generic (no per-tool flavor) —
// spawn_subagent is the one we call out, since it's a sub-agent running.
export function activityLabel(ev: FabriEvent): string {
  if (ev.name === SPAWN_SUBAGENT) return "Delegating to a sub-agent";
  if (ev.type === EventType.PARALLEL_GROUP_STARTED) {
    const calls = Array.isArray(ev.calls) ? ev.calls.length : undefined;
    return calls ? `Running ${calls} tools in parallel` : "Running tools in parallel";
  }
  return ev.name ? ev.name : "Working";
}

export function isSubagent(ev: FabriEvent): boolean {
  return ev.name === SPAWN_SUBAGENT;
}

// Tool-call status derived from the paired `tool_call` result. A card with no
// paired result yet is still running.
export type ToolStatus = "running" | "ok" | "error";

export function toolStatus(result: FabriEvent | undefined): ToolStatus {
  if (!result) return "running";
  const ok = result.result?.ok;
  if (ok === true) return "ok";
  if (ok === false) return "error";
  // A tool_call with no explicit ok (non-subagent builtin) is treated as done-ok.
  return "ok";
}

// Wall time between a tool_started and its paired tool_call, in seconds, or null
// when either timestamp is missing.
export function toolDuration(
  started: FabriEvent | undefined,
  finished: FabriEvent | undefined,
): number | null {
  const a = started?.ts;
  const b = finished?.ts;
  if (typeof a !== "number" || typeof b !== "number" || b < a) return null;
  return b - a;
}
