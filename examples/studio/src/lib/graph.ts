import { EventType, isSubagent, type FabriEvent } from "./events";

export interface AgentNode {
  id: string;
  label: string;
  kind: "manager" | "specialist";
  emoji: string;
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

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function textValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function emojiFor(label: string): string {
  const value = label.toLowerCase();
  if (value.includes("research") || value.includes("analy")) return "🧑‍🔬";
  if (value.includes("writ") || value.includes("author") || value.includes("draft")) return "✍️";
  if (["verif", "review", "qa", "check", "test"].some((part) => value.includes(part))) return "🕵️";
  if (value.includes("plan") || value.includes("architect")) return "🗺️";
  if (value.includes("design")) return "🎨";
  if (["code", "build", "engineer", "dev"].some((part) => value.includes(part))) return "🧑‍💻";
  if (value.includes("data")) return "📊";
  return "🤖";
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

function childResult(ev: FabriEvent | undefined): Record<string, unknown> | undefined {
  return recordValue(ev?.result);
}

function isAgentHandoff(ev: FabriEvent): boolean {
  if (ev.name === "ask_user") return false;
  const result = childResult(ev);
  return isSubagent(ev) || result?.session_id != null || result?.trace_path != null;
}

function handoffStatus(result: FabriEvent | undefined): Handoff["status"] {
  if (!result) return "running";
  const data = childResult(result);
  if (data?.ok === false) return "error";
  const outcome = textValue(data?.outcome);
  return outcome && !SUCCESS_OUTCOMES.has(outcome) ? "error" : "done";
}

function usageFor(ev: FabriEvent | undefined): Usage | undefined {
  return recordValue(childResult(ev)?.usage) as Usage | undefined;
}

function childCost(ev: FabriEvent | undefined): number {
  const result = childResult(ev);
  const usage = usageFor(ev);
  return numberValue(usage?.cost_usd) || numberValue(usage?.total_cost_usd) || numberValue(result?.cost_usd);
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
    id: "manager", label: managerLabel ?? "Manager", kind: "manager", emoji: "🧑‍💼",
    status: "running", costUsd: 0, callCount: 1, order: 0,
  };
  nodes.set(manager.id, manager);

  const addHandoff = (begin: FabriEvent, finish?: FabriEvent) => {
    const source = finish ?? begin;
    if (!isAgentHandoff(source)) return;
    const label = labelFor(source.type === EventType.TOOL_CALL ? source : begin);
    const id = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || "sub-agent";
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
        id, label, kind: "specialist", emoji: emojiFor(label), status, costUsd,
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
