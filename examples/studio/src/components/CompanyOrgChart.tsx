import { useMemo } from "react";
import type { Company, CompanyNode } from "../lib/api";
import type { FabriEvent } from "../lib/events";
import {
  activityKey,
  deriveNodeActivity,
  type NodeActivity,
  type NodeActivityMap,
  type NodeStatus,
} from "../lib/companyActivity";

// The multi-level company view: a top-down org chart of a served company
// (ceo → VPs → crews), drawn straight from GET /company so it's always correct
// without a live run. When a run IS streaming, each node lights up with its live
// status and real cost (see lib/companyActivity), matched from handoffs by id.

function money(v: number): string {
  return `$${v.toFixed(4)}`;
}

function NodeCard({ node, activity }: { node: CompanyNode; activity?: NodeActivity }) {
  const status: NodeStatus = activity?.status ?? "idle";
  const live = activity && (activity.costUsd > 0 || status === "running" || status === "done");
  return (
    <div className={`org2-node org2-node--${node.kind} org2-node--${status}`}>
      <div className="org2-node__role">
        <span className="org2-node__dot" aria-hidden />
        {node.kind === "manager" ? "Manager" : "Crew"}
      </div>
      <div className="org2-node__title">{node.title}</div>
      {node.agency && <div className="org2-node__agency">{node.agency}</div>}
      {live && (
        <div className="org2-node__cost">{activity!.costUsd > 0 ? money(activity!.costUsd) : status}</div>
      )}
    </div>
  );
}

function Subtree({
  id,
  byId,
  activity,
}: {
  id: string;
  byId: Map<string, CompanyNode>;
  activity: NodeActivityMap;
}) {
  const node = byId.get(id);
  if (!node) return null;
  const act = activity.get(activityKey(node.id)) ?? activity.get(activityKey(node.title));
  return (
    <li>
      <NodeCard node={node} activity={act} />
      {node.children.length > 0 && (
        <ul>
          {node.children.map((childId) => (
            <Subtree key={childId} id={childId} byId={byId} activity={activity} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function CompanyOrgChart({ company, events }: { company: Company; events?: FabriEvent[] }) {
  const byId = useMemo(() => new Map(company.nodes.map((n) => [n.id, n] as const)), [company]);
  const activity = useMemo(() => deriveNodeActivity(events), [events]);
  const crews = company.nodes.filter((n) => n.kind === "crew").length;
  const totalCost = useMemo(
    () => [...activity.values()].reduce((sum, a) => sum + (a.costUsd || 0), 0),
    [activity],
  );

  return (
    <section className="org2" aria-label={`${company.title} org chart`}>
      <div className="org2-head">
        <h2 className="org2-title">{company.title}</h2>
        <span className="org2-meta">
          {company.nodes.length} agents · {crews} crews
          {company.max_cost_usd != null && ` · $${company.max_cost_usd}/run ceiling`}
          {totalCost > 0 && <span className="org2-meta__spent"> · spent {money(totalCost)}</span>}
        </span>
      </div>
      {company.positioning && <p className="org2-positioning">{company.positioning}</p>}
      <div className="org2-scroll">
        <ul className="org2-tree">
          <Subtree id={company.root_id} byId={byId} activity={activity} />
        </ul>
      </div>
    </section>
  );
}
