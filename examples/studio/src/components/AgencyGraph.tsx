import { useEffect, useMemo, useRef, useState } from "react";
import { buildAgencyGraph, type AgencyGraph, type AgentNode, type Handoff } from "../lib/graph";
import type { FabriEvent } from "../lib/events";

function money(value: number): string {
  return `$${value.toFixed(4)}`;
}

function tokens(node: AgentNode): string | null {
  if (node.inputTokens == null && node.outputTokens == null) return null;
  return `${node.inputTokens ?? 0} in / ${node.outputTokens ?? 0} out`;
}

function statusSymbol(status: AgentNode["status"]): string | null {
  return status === "done" ? "✅" : status === "error" ? "❌" : null;
}

function OfficeAvatar({ node }: { node: AgentNode }) {
  const symbol = statusSymbol(node.status);
  return (
    <div className={`office-agent office-agent--${node.status}`}>
      <div className="office-agent__avatar" aria-label={`${node.label}: ${node.status}`}>
        <span>{node.emoji}</span>
        {symbol && <span className="office-agent__result">{symbol}</span>}
        {node.status === "running" && <span className="office-agent__thinking">···</span>}
      </div>
      <div className="office-agent__label">{node.label}</div>
      <div className="office-agent__meta">
        {money(node.costUsd)} {node.callCount > 1 && <span className="office-agent__count">×{node.callCount}</span>}
      </div>
    </div>
  );
}

function AgencyOffice({ graph }: { graph: AgencyGraph }) {
  const manager = graph.nodes[0];
  const specialists = graph.nodes.slice(1);
  const runningTargets = new Set(graph.edges.filter((edge) => edge.status === "running").map((edge) => edge.toId));
  return (
    <section className="office" aria-label="Office view">
      <div className="office-manager"><OfficeAvatar node={manager} /></div>
      <div className="office-team">
        {specialists.map((node) => (
          <div className={`office-team__desk${node.parallelGroup ? " office-team__desk--concurrent" : ""}`} key={node.id}>
            <span className="office-team__connector" aria-hidden />
            {runningTargets.has(node.id) && <span className="office-team__document" aria-hidden>📄</span>}
            <OfficeAvatar node={node} />
          </div>
        ))}
      </div>
    </section>
  );
}

function NodeCard({ node, edge }: { node: AgentNode; edge?: Handoff }) {
  const tooltip = edge?.fullTask
    ? `${edge.fullTask}${edge.outcome ? `\nOutcome: ${edge.outcome}` : ""}`
    : undefined;
  return (
    <article className={`org-node org-node--${node.status}`} title={tooltip}>
      <div className="org-node__head">
        <span className="org-node__emoji" aria-hidden>{node.emoji}</span>
        <span className="org-node__label">{node.label}</span>
        <span className={`org-status org-status--${node.status}`}>{node.status}</span>
      </div>
      <div className="org-node__metrics">
        <span>{money(node.costUsd)}</span>
        {tokens(node) && <span>{tokens(node)}</span>}
        {node.callCount > 1 && <span>×{node.callCount}</span>}
      </div>
      {edge?.fullTask && <div className="org-node__detail">{edge.fullTask}{edge.outcome && ` · ${edge.outcome}`}</div>}
    </article>
  );
}

function AgencyOrgChart({ graph }: { graph: AgencyGraph }) {
  const manager = graph.nodes[0];
  const specialists = graph.nodes.slice(1);
  return (
    <section className="org-chart" aria-label="Org chart view">
      <NodeCard node={manager} />
      <div className="org-chart__specialists">
        {specialists.map((node) => {
          const nodeEdges = graph.edges.filter((edge) => edge.toId === node.id);
          return (
            <div className={`org-chart__branch${node.parallelGroup ? " org-chart__branch--concurrent" : ""}`} key={node.id}>
              <span className="org-chart__connector" aria-hidden />
              <NodeCard node={node} edge={nodeEdges[nodeEdges.length - 1]} />
              <div className="org-chart__memos">
                {nodeEdges.map((edge) => (
                  <span className={`org-edge org-edge--${edge.status}`} key={edge.id} title={`${edge.fullTask ?? edge.tool}${edge.outcome ? `\nOutcome: ${edge.outcome}` : ""}`}>
                    {edge.task ?? edge.tool}
                  </span>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function AgencyGraph({ events, managerLabel }: { events: FabriEvent[]; managerLabel?: string }) {
  const graph = useMemo(() => buildAgencyGraph(events, managerLabel), [events, managerLabel]);
  const [view, setView] = useState<"office" | "org">("office");
  const previousPayroll = useRef(graph.payrollUsd);
  const [coinDropping, setCoinDropping] = useState(false);

  useEffect(() => {
    if (graph.payrollUsd > previousPayroll.current) {
      setCoinDropping(true);
      const timeout = window.setTimeout(() => setCoinDropping(false), 560);
      previousPayroll.current = graph.payrollUsd;
      return () => window.clearTimeout(timeout);
    }
    previousPayroll.current = graph.payrollUsd;
  }, [graph.payrollUsd]);

  return (
    <section className="agency-graph">
      <header className="agency-graph__head">
        <div className="agency-graph__views" role="group" aria-label="Company view">
          <button className={`agency-graph__view tab${view === "office" ? " tab--on" : ""}`} onClick={() => setView("office")}>Office</button>
          <button className={`agency-graph__view tab${view === "org" ? " tab--on" : ""}`} onClick={() => setView("org")}>Org chart</button>
        </div>
        <div className="agency-payroll"><span className={coinDropping ? "agency-payroll__coin agency-payroll__coin--drop" : "agency-payroll__coin"}>🪙</span> payroll: <strong>{money(graph.payrollUsd)}</strong></div>
      </header>
      {!graph.hasAnyHandoff ? (
        <p className="agency-empty">The manager hasn't brought anyone in yet — waiting for the team to get to work.</p>
      ) : view === "office" ? <AgencyOffice graph={graph} /> : <AgencyOrgChart graph={graph} />}
    </section>
  );
}
