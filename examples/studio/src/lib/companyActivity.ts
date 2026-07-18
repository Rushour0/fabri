import type { FabriEvent } from "./events";
import { buildAgencyGraph } from "./graph";

// Live-run overlay for the company org chart: per-node status + real cost,
// derived from a run's handoffs. Kept here (not inline in the component) so the
// types stay exported and reusable, and the derivation can be unit-tested apart
// from the view.

export type NodeStatus = "idle" | "running" | "done" | "error";

export interface NodeActivity {
  status: NodeStatus;
  costUsd: number;
  calls: number;
}

// Map keyed by a normalized node id/label → live activity for that node.
export type NodeActivityMap = Map<string, NodeActivity>;

// Normalize a node id or a humanized label to a common key ("VP Eng" → "vp_eng"),
// so a run graph's nodes match a company's node ids.
export function activityKey(idOrLabel: string): string {
  return idOrLabel.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
}

// Node ids equal the compiled agent names, so we key the run graph's nodes by a
// normalized label and match them to company node ids. Best-effort: on any shape
// mismatch this returns an empty map and the chart stays static (still correct).
export function deriveNodeActivity(events: FabriEvent[] | undefined): NodeActivityMap {
  const map: NodeActivityMap = new Map();
  if (!events || !events.length) return map;
  try {
    const graph = buildAgencyGraph(events, "");
    for (const node of graph.nodes) {
      map.set(activityKey(node.label || node.id), {
        status: node.status as NodeStatus,
        costUsd: node.costUsd,
        calls: node.callCount ?? 0,
      });
    }
  } catch {
    /* static-only fallback */
  }
  return map;
}
