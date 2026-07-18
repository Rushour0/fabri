import { describe, it, expect } from "vitest";
import { buildAgencyGraph, humanizeAgentLabel } from "./graph";
import type { FabriEvent } from "./events";

// A fabri tool_call's `result` is the canonical envelope {ok, result, error}
// (src/fabri/tools/result.py); a sub-agent's own return — session_id, outcome,
// usage — is nested ONE LEVEL under `result`. These builders mirror that real
// wire shape so the tests guard against the graph reading it too shallow (the
// bug that made static specialists vanish and dynamic nodes show $0).
function toolStarted(step: number, callIndex: number, name: string, args: Record<string, unknown> = {}): FabriEvent {
  return { type: "tool_started", step, call_index: callIndex, name, args, ts: step };
}
function agentCall(
  step: number,
  callIndex: number,
  name: string,
  child: { session_id?: string; outcome?: string; cost?: number; total?: number; in?: number; out?: number },
  opts: { ok?: boolean; args?: Record<string, unknown>; parallel_group?: string } = {},
): FabriEvent {
  const usage: Record<string, number> = {};
  if (child.cost !== undefined) usage.cost_usd = child.cost;
  if (child.total !== undefined) usage.total_cost_usd = child.total;
  if (child.in !== undefined) usage.input_tokens = child.in;
  if (child.out !== undefined) usage.output_tokens = child.out;
  return {
    type: "tool_call",
    step,
    call_index: callIndex,
    name,
    args: opts.args ?? {},
    parallel_group: opts.parallel_group,
    result: {
      ok: opts.ok ?? true,
      result: {
        session_id: child.session_id ?? `sess-${name}-${step}`,
        trace_path: `traces/${child.session_id ?? name}.jsonl`,
        outcome: child.outcome ?? "success",
        final_text: "done",
        usage,
      },
    },
    ts: step + 0.5,
  };
}
const usageEvent = (cost: number): FabriEvent => ({ type: "usage", cost_usd: cost, total_cost_usd: cost + 99, ts: 100 });
const final = (): FabriEvent => ({ type: "final", text: "done", outcome: "success", ts: 101 });

describe("buildAgencyGraph — nested envelope contract", () => {
  it("reads child cost/tokens/outcome from the NESTED result envelope (regression)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_research"),
      agentCall(1, 0, "release_research", { outcome: "success", total: 0.0011, in: 1400, out: 260 }),
      final(),
    ];
    const g = buildAgencyGraph(events);
    const node = g.nodes.find((n) => n.label === "Release Research");
    expect(node).toBeDefined();
    expect(node!.costUsd).toBeCloseTo(0.0011, 6); // NOT 0 — proves nested read
    expect(node!.inputTokens).toBe(1400);
    expect(node!.outputTokens).toBe(260);
    expect(node!.status).toBe("done");
  });

  it("detects a static specialist by its nested session_id (no spawn_subagent name)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_writer"),
      agentCall(1, 0, "release_writer", { total: 0.002 }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.hasAnyHandoff).toBe(true);
    expect(g.nodes.some((n) => n.label === "Release Writer")).toBe(true);
  });

  it("ignores a plain manager tool (no nested session_id/trace_path)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "write_file"),
      { type: "tool_call", step: 1, call_index: 0, name: "write_file", args: {}, result: { ok: true, result: { path: "x.md", bytes: 12 } }, ts: 1.5 },
    ];
    const g = buildAgencyGraph(events);
    expect(g.hasAnyHandoff).toBe(false);
    expect(g.nodes).toHaveLength(1); // manager only
  });

  it("labels a spawn_subagent from args.role and costs it from the nested usage", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "spawn_subagent", { role: "market_researcher" }),
      agentCall(1, 0, "spawn_subagent", { total: 0.0031, in: 2100, out: 700 }, { args: { role: "market_researcher" } }),
    ];
    const g = buildAgencyGraph(events);
    const node = g.nodes.find((n) => n.label === "Market Researcher");
    expect(node).toBeDefined();
    expect(node!.costUsd).toBeCloseTo(0.0031, 6);
    expect(node!.emoji).toBe("🧑‍🔬");
  });
});

describe("buildAgencyGraph — dedupe, status, payroll", () => {
  it("dedupes a repair loop into one node (×N) summing cost/tokens, one edge per call", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_writer"),
      agentCall(1, 0, "release_writer", { total: 0.0022, in: 1900, out: 640 }),
      toolStarted(2, 0, "release_writer"),
      agentCall(2, 0, "release_writer", { total: 0.0019, in: 1500, out: 480 }),
    ];
    const g = buildAgencyGraph(events);
    const node = g.nodes.find((n) => n.label === "Release Writer")!;
    expect(node.callCount).toBe(2);
    expect(node.costUsd).toBeCloseTo(0.0041, 6);
    expect(node.inputTokens).toBe(3400);
    expect(g.edges.filter((e) => e.toId === node.id)).toHaveLength(2);
  });

  it("node status reflects the LATEST resolved call (repaired verifier reads done, not error)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_verifier"),
      agentCall(1, 0, "release_verifier", { outcome: "failure", total: 0.0008 }),
      toolStarted(2, 0, "release_verifier"),
      agentCall(2, 0, "release_verifier", { outcome: "success", total: 0.0008 }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.nodes.find((n) => n.label === "Release Verifier")!.status).toBe("done");
  });

  it("marks a dispatch failure (envelope ok=false) as error", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "spawn_subagent", { role: "pricer" }),
      agentCall(1, 0, "spawn_subagent", { total: 0.001 }, { ok: false, args: { role: "pricer" } }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.nodes.find((n) => n.label === "Pricer")!.status).toBe("error");
  });

  it("marks a child that exited ok but with a non-success outcome as error", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "spawn_subagent", { role: "pricer" }),
      agentCall(1, 0, "spawn_subagent", { outcome: "failure", total: 0.001 }, { ok: true, args: { role: "pricer" } }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.nodes.find((n) => n.label === "Pricer")!.status).toBe("error");
  });

  it("honors an explicit total_cost_usd of 0 (no truthiness fallback)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_writer"),
      agentCall(1, 0, "release_writer", { cost: 0.5, total: 0 }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.nodes.find((n) => n.label === "Release Writer")!.costUsd).toBe(0);
  });

  it("payroll = manager's bare cost_usd + Σ child costs (no double-count of total_cost_usd)", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "release_research"),
      agentCall(1, 0, "release_research", { total: 0.001 }),
      toolStarted(2, 0, "release_writer"),
      agentCall(2, 0, "release_writer", { total: 0.002 }),
      usageEvent(0.0005), // manager: cost_usd 0.0005, total_cost_usd 99.0005 (must be ignored)
      final(),
    ];
    const g = buildAgencyGraph(events);
    expect(g.payrollUsd).toBeCloseTo(0.0035, 6);
  });
});

describe("buildAgencyGraph — ids, parallelism, edge cases", () => {
  it("namespaces specialist ids so a sub-agent named 'Manager' can't clobber the root", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "spawn_subagent", { role: "manager" }),
      agentCall(1, 0, "spawn_subagent", { total: 0.001 }, { args: { role: "manager" } }),
    ];
    const g = buildAgencyGraph(events);
    const root = g.nodes.find((n) => n.kind === "manager")!;
    const specialist = g.nodes.find((n) => n.kind === "specialist")!;
    expect(g.nodes).toHaveLength(2);
    expect(root.id).toBe("manager");
    expect(specialist.id).not.toBe("manager");
    expect(root.callCount).toBe(1); // root not mutated by the specialist
  });

  it("tags concurrent siblings with their parallel_group", () => {
    const events: FabriEvent[] = [
      toolStarted(1, 0, "spawn_subagent", { role: "a" }),
      toolStarted(1, 1, "spawn_subagent", { role: "b" }),
      agentCall(1, 0, "spawn_subagent", { total: 0.001 }, { args: { role: "a" }, parallel_group: "g1" }),
      agentCall(1, 1, "spawn_subagent", { total: 0.001 }, { args: { role: "b" }, parallel_group: "g1" }),
    ];
    const g = buildAgencyGraph(events);
    expect(g.nodes.filter((n) => n.parallelGroup === "g1")).toHaveLength(2);
  });

  it("shows a still-running handoff (tool_started, no tool_call) as running with 0 cost", () => {
    const g = buildAgencyGraph([toolStarted(1, 0, "spawn_subagent", { role: "worker" })]);
    const node = g.nodes.find((n) => n.label === "Worker")!;
    expect(node.status).toBe("running");
    expect(node.costUsd).toBe(0);
  });

  it("returns only the manager node with no handoffs for empty events", () => {
    const g = buildAgencyGraph([]);
    expect(g.hasAnyHandoff).toBe(false);
    expect(g.nodes).toHaveLength(1);
    expect(g.nodes[0].kind).toBe("manager");
  });
});

describe("role emoji mapping", () => {
  const emojiOf = (role: string) => {
    const g = buildAgencyGraph([
      toolStarted(1, 0, "spawn_subagent", { role }),
      agentCall(1, 0, "spawn_subagent", { total: 0.001 }, { args: { role } }),
    ]);
    return g.nodes.find((n) => n.kind === "specialist")!.emoji;
  };
  it("maps common crew roles to distinct icons", () => {
    expect(emojiOf("bug_triager")).toBe("🔍");
    expect(emojiOf("bug_fixer")).toBe("🔧");
    expect(emojiOf("bug_tester")).toBe("🧪");
    expect(emojiOf("market_researcher")).toBe("🧑‍🔬");
    expect(emojiOf("release_writer")).toBe("✍️");
    expect(emojiOf("something_unmapped")).toBe("🤖");
  });
});

describe("humanizeAgentLabel", () => {
  it("titlecases snake, kebab, and camel forms", () => {
    expect(humanizeAgentLabel("release_writer")).toBe("Release Writer");
    expect(humanizeAgentLabel("competitor-analyst")).toBe("Competitor Analyst");
    expect(humanizeAgentLabel("marketResearcher")).toBe("Market Researcher");
  });
});
