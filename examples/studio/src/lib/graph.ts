import { EventType, isSubagent, type FabriEvent } from "./events";

export interface AgentNode {
  id: string;
  label: string;
  kind: "manager" | "specialist";
  /** Role key for the Company-view icon set (see agentIcons.tsx). */
  icon: string;
  status: "idle" | "running" | "done" | "error";
  costUsd: number;
  inputTokens?: number;
  outputTokens?: number;
  callCount: number;
  parallelGroup?: string;
  order: number;
}

export interface Handoff {
  id: string;
  fromId: string;
  toId: string;
  tool: string;
  task?: string;
  /** Full task text is retained for the org-chart hover detail. */
  fullTask?: string;
  status: "running" | "done" | "error";
  costUsd: number;
  outcome?: string;
  parallelGroup?: string;
  callIndex?: number;
  startedTs?: number;
  finishedTs?: number;
}

export interface AgencyGraph {
  nodes: AgentNode[];
  edges: Handoff[];
  payrollUsd: number;
  hasAnyHandoff: boolean;
}

type Usage = { cost_usd?: unknown; total_cost_usd?: unknown; input_tokens?: unknown; output_tokens?: unknown };

const SUCCESS_OUTCOMES = new Set(["success", "success_with_recovery"]);

export function humanizeAgentLabel(raw: string): string {
  const words = raw
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[_-]+/g, " ")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  return words.map((word) => word.charAt(0).toUpperCase() + word.slice(1)).join(" ") || "Sub-agent";
}

function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

// First finite number among the candidates (honoring an explicit 0), else undefined.
function firstNumber(...values: unknown[]): number | undefined {
  for (const v of values) if (typeof v === "number" && Number.isFinite(v)) return v;
  return undefined;
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function textValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

// Role → icon key (rendered by agentIcons.tsx), matched by substring (first hit
// wins). Ordered most-specific first so e.g. "test" beats the generic code bucket.
const ROLE_ICONS: [string[], string][] = [
  [["triage", "localiz", "diagnos", "investigat"], "triage"],
  [["fix", "patch", "repair", "debug", "hotfix"], "fix"],
  [["research", "analy", "inspect"], "research"],
  [["writ", "author", "draft", "summ", "doc", "content"], "write"],
  [["test", "qa"], "test"],
  [["verif", "review", "check", "audit", "lint"], "verify"],
  [["plan", "architect", "orchestrat", "coordinat"], "plan"],
  [["design", "ux", "ui"], "design"],
  [["deploy", "release", "ship", "publish"], "deploy"],
  [["secur", "threat", "vuln"], "security"],
  [["data", "sql", "etl"], "data"],
  [["code", "build", "engineer", "dev", "implement"], "code"],
];

function iconFor(label: string): string {
  const value = label.toLowerCase();
  for (const [parts, key] of ROLE_ICONS) {
    if (parts.some((part) => value.includes(part))) return key;
  }
  return "agent";
}

function labelFor(ev: FabriEvent): string {
  if (!isSubagent(ev)) return humanizeAgentLabel(ev.name ?? "Sub-agent");
  const args = ev.args ?? {};
  const named = textValue(args.role) ?? textValue(args.label) ?? textValue(args.name);
  if (named) return humanizeAgentLabel(named);
  const task = textValue(args.task);
  return task ? humanizeAgentLabel(task.split(/[.:,]/)[0].slice(0, 48)) : "Sub-agent";
}

function taskFor(ev: FabriEvent | undefined): string | undefined {
  if (!ev) return undefined;
  const args = ev.args ?? {};
  return textValue(args.task) ?? textValue(args.input) ?? (Object.keys(args).length ? JSON.stringify(args) : undefined);
}

function truncateTask(task: string | undefined): string | undefined {
  if (!task) return undefined;
  return task.length > 60 ? `${task.slice(0, 57)}…` : task;
}

// A tool_call's `result` is the canonical envelope `{ok, result, error}`
// (src/fabri/tools/result.py). A sub-agent's own return — session_id, outcome,
// usage, … — is nested one level under `result`. Read `ok` off the envelope but
// everything else off the nested child.
function envelope(ev: FabriEvent | undefined): Record<string, unknown> | undefined {
  return recordValue(ev?.result);
}

function childResult(ev: FabriEvent | undefined): Record<string, unknown> | undefined {
  const env = envelope(ev);
  const nested = recordValue(env?.result);
  // Prefer the nested child payload; fall back to the envelope for any tool that
  // returns its payload flat (keeps detection robust to shape drift).
  return nested ?? env;
}

function isAgentHandoff(ev: FabriEvent): boolean {
  if (ev.name === "ask_user") return false;
  const child = childResult(ev);
  return isSubagent(ev) || child?.session_id != null || child?.trace_path != null;
}

function handoffStatus(ev: FabriEvent | undefined): Handoff["status"] {
  if (!ev) return "running";
  // Envelope ok=false is a dispatch failure; otherwise a child that exited 0 can
  // still carry a worse-than-success outcome, which reads as error.
  if (envelope(ev)?.ok === false) return "error";
  const outcome = textValue(childResult(ev)?.outcome);
  return outcome && !SUCCESS_OUTCOMES.has(outcome) ? "error" : "done";
}

function usageFor(ev: FabriEvent | undefined): Usage | undefined {
  return recordValue(childResult(ev)?.usage) as Usage | undefined;
}

function childCost(ev: FabriEvent | undefined): number {
  const usage = usageFor(ev);
  const child = childResult(ev);
  // Per-node COGS = the child's end-to-end cost. total_cost_usd includes its own
  // grandchildren (matches fabri's parent rollup); nullish so an explicit 0 wins.
  return firstNumber(usage?.total_cost_usd, usage?.cost_usd, child?.cost_usd) ?? 0;
}

function nodeStatus(current: AgentNode["status"], incoming: Handoff["status"]): AgentNode["status"] {
  // A call still in flight dominates — the agent is actively working.
  if (incoming === "running" || current === "running") return "running";
  // Otherwise the latest resolved call reflects the agent's current state: a
  // verify→repair→verify loop that ultimately passes reads as done, not error.
  // Per-call edges still carry each attempt's individual pass/fail.
  return incoming;
}

export function buildAgencyGraph(events: FabriEvent[], managerLabel?: string): AgencyGraph {
  const nodes = new Map<string, AgentNode>();
  const edges: Handoff[] = [];
  const started = new Map<string, FabriEvent>();
  let managerStatus: AgentNode["status"] = "running";
  let managerCost = 0;
  let invocationOrder = 1;

  const manager: AgentNode = {
    id: "manager", label: managerLabel ?? "Manager", kind: "manager", icon: "manager",
    status: "running", costUsd: 0, callCount: 1, order: 0,
  };
  nodes.set(manager.id, manager);

  const addHandoff = (begin: FabriEvent, finish?: FabriEvent) => {
    const source = finish ?? begin;
    if (!isAgentHandoff(source)) return;
    const label = labelFor(source.type === EventType.TOOL_CALL ? source : begin);
    // Namespace specialist ids so one literally labeled "Manager" can't collide
    // with the reserved root node id.
    const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || "sub-agent";
    const id = `agent-${slug}`;
    const status = handoffStatus(finish);
    const costUsd = childCost(finish);
    const usage = usageFor(finish);
    const fullTask = taskFor(begin) ?? taskFor(finish);
    const outcome = textValue(childResult(finish)?.outcome);
    const edge: Handoff = {
      id: `${begin.step ?? "?"}:${begin.call_index ?? edges.length}`, fromId: "manager", toId: id,
      tool: source.name ?? "spawn_subagent", task: truncateTask(fullTask), fullTask, status, costUsd, outcome,
      parallelGroup: begin.parallel_group ?? finish?.parallel_group, callIndex: begin.call_index ?? finish?.call_index,
      startedTs: begin.ts, finishedTs: finish?.ts,
    };
    edges.push(edge);
    const existing = nodes.get(id);
    if (existing) {
      existing.status = nodeStatus(existing.status, status);
      existing.costUsd += costUsd;
      existing.callCount += 1;
      existing.inputTokens = (existing.inputTokens ?? 0) + numberValue(usage?.input_tokens);
      existing.outputTokens = (existing.outputTokens ?? 0) + numberValue(usage?.output_tokens);
      existing.parallelGroup ??= edge.parallelGroup;
    } else {
      nodes.set(id, {
        id, label, kind: "specialist", icon: iconFor(label), status, costUsd,
        inputTokens: numberValue(usage?.input_tokens) || undefined,
        outputTokens: numberValue(usage?.output_tokens) || undefined,
        callCount: 1, parallelGroup: edge.parallelGroup, order: invocationOrder++,
      });
    }
  };

  for (const ev of events) {
    if (ev.type === EventType.USAGE) managerCost += numberValue(ev.cost_usd);
    if (ev.type === EventType.FINAL) managerStatus = "done";
    if (ev.type === EventType.FAILED || ev.type === EventType.INCOMPLETE || ev.type === EventType.ERROR) managerStatus = "error";
    if (ev.type !== EventType.TOOL_STARTED && ev.type !== EventType.TOOL_CALL) continue;
    const key = `${ev.step ?? "?"}:${ev.call_index ?? "?"}`;
    if (ev.type === EventType.TOOL_STARTED) {
      started.set(key, ev);
      continue;
    }
    const begin = started.get(key);
    if (begin) started.delete(key);
    addHandoff(begin ?? ev, ev);
  }
  for (const begin of started.values()) addHandoff(begin);

  manager.status = managerStatus;
  manager.costUsd = managerCost;
  const nodeList = [...nodes.values()].sort((a, b) => a.order - b.order);
  return {
    nodes: nodeList,
    edges,
    payrollUsd: managerCost + edges.reduce((total, edge) => total + edge.costUsd, 0),
    hasAnyHandoff: edges.length > 0,
  };
}
