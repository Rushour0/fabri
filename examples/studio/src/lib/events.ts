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
  FINAL: "final",
  FAILED: "failed",
  INCOMPLETE: "incomplete",
  ERROR: "error",
  ASK_USER: "ask_user",
  USAGE: "usage",
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
  result?: Record<string, unknown>;
  call_index?: number;
  parallel_group?: number;
  trigger?: string; // narration
  // ask_user (listener-injected — see the HITL backend slice)
  question_id?: string;
  question?: string;
  options?: string[];
  default?: string;
  // usage / cost
  cost_usd?: number;
  total_cost_usd?: number;
  input_tokens?: number;
  output_tokens?: number;
  step_count?: number;
  wall_time_s?: number;
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
  cost?: { total_cost_usd?: number; [k: string]: unknown } | null;
  error?: string | null;
}

// Studio's message taxonomy — how each fabri event class renders. The mapping
// from event → class is the core UI contract (see PM handoff).
export type MessageClass =
  | "manager" //   final              → the manager's definitive message (primary)
  | "narration" // narration          → light "what's happening now" line
  | "thought" //   thought            → collapsible main-model reasoning
  | "activity" //  tool_started/call, spawn_subagent, parallel_group → sub-agent/tool chip
  | "ask" //       ask_user           → interactive question card (primary)
  | "cost" //      usage              → cost / COGS footer
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
    case EventType.ASK_USER:
      return "ask";
    case EventType.USAGE:
      return "cost";
    case EventType.FAILED:
    case EventType.INCOMPLETE:
    case EventType.ERROR:
      return "terminal";
    case EventType.TOOL_STARTED:
    case EventType.TOOL_CALL:
      // ask_user surfaces as its own interactive card — don't also show a
      // generic "running ask_user" activity chip for it.
      return ev.name === "ask_user" ? null : "activity";
    case EventType.PARALLEL_GROUP_STARTED:
      return "activity";
    // Lifecycle / plumbing — not surfaced as messages.
    case EventType.START:
    case EventType.STEP_STARTED:
    case EventType.STEP_FINISHED:
    case EventType.RETRIEVAL:
    case EventType.STRUCTURED_OUTPUT:
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
    const calls = Array.isArray(ev.calls) ? (ev.calls as unknown[]).length : undefined;
    return calls ? `Running ${calls} tools in parallel` : "Running tools in parallel";
  }
  return ev.name ? `Running ${ev.name}` : "Working";
}

export function isSubagent(ev: FabriEvent): boolean {
  return ev.name === SPAWN_SUBAGENT;
}
