import type { CostSurface, FabriEvent } from "../lib/events";

// The run's COGS + metrics receipt. Fabri already computes all of this
// (src/fabri/pricing.py + the usage event); Studio used to render only the
// total and drop the per-model breakdown, token counts, and sub-agent split.
// This surfaces the whole thing — the "aggregate cost was captured but never
// shown" gap. Fed either the live `usage` event or the terminal result's
// CostSurface; both carry the same fields.

interface Normalized {
  total?: number;
  own?: number;
  subagent?: number;
  byModel: [string, number][];
  inputTokens?: number;
  outputTokens?: number;
  steps?: number;
  wall?: number;
  subagentCount?: number;
  subagentFailed?: number;
}

function fromEvent(ev: FabriEvent): Normalized {
  return {
    total: ev.total_cost_usd ?? ev.cost_usd,
    own: ev.cost_usd,
    subagent: ev.subagent_cost_usd,
    byModel: Object.entries(ev.cost_by_model ?? {}),
    inputTokens: ev.input_tokens,
    outputTokens: ev.output_tokens,
    steps: ev.step_count,
    wall: ev.wall_time_s,
    subagentCount: ev.subagent_count,
    subagentFailed: ev.subagent_failed_count,
  };
}

function fromSurface(c: CostSurface): Normalized {
  const m = c.metrics ?? {};
  return {
    total: c.total_cost_usd,
    own: c.cost_usd,
    subagent: c.subagent_cost_usd,
    byModel: Object.entries(c.cost_by_model ?? {}),
    inputTokens: m.input_tokens,
    outputTokens: m.output_tokens,
    steps: m.step_count,
    wall: m.wall_time_s,
    subagentCount: m.subagent_count,
    subagentFailed: m.subagent_failed_count,
  };
}

function money(n: number | undefined): string {
  if (n == null) return "—";
  return "$" + (n < 0.01 ? n.toFixed(5) : n.toFixed(4));
}

function tokens(n: number | undefined): string {
  if (n == null) return "—";
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

export function CostSummary({
  event,
  surface,
  maxCostUsd,
}: {
  event?: FabriEvent;
  surface?: CostSurface;
  maxCostUsd?: number;
}) {
  const n = event ? fromEvent(event) : surface ? fromSurface(surface) : null;
  if (!n) return null;

  const overBudget = maxCostUsd != null && n.total != null && n.total > maxCostUsd;
  const budgetPct =
    maxCostUsd != null && maxCostUsd > 0 && n.total != null
      ? Math.min(100, Math.round((n.total / maxCostUsd) * 100))
      : null;

  return (
    <section className="cogs" aria-label="run cost and metrics">
      <div className="cogs__head">
        <span className="cogs__label">COGS</span>
        <span className={"cogs__total" + (overBudget ? " cogs__total--over" : "")}>
          {money(n.total)}
        </span>
        {budgetPct != null && (
          <span className={"cogs__budget" + (overBudget ? " cogs__budget--over" : "")}>
            {budgetPct}% of {money(maxCostUsd)} budget
          </span>
        )}
      </div>

      {n.byModel.length > 0 && (
        <ul className="cogs__models">
          {n.byModel.map(([model, cost]) => (
            <li key={model} className="cogs__model">
              <span className="cogs__model-name">{model}</span>
              <span className="cogs__model-cost">{money(cost)}</span>
            </li>
          ))}
          {n.subagent != null && n.subagent > 0 && (
            <li className="cogs__model cogs__model--sub">
              <span className="cogs__model-name">sub-agents</span>
              <span className="cogs__model-cost">{money(n.subagent)}</span>
            </li>
          )}
        </ul>
      )}

      <dl className="cogs__metrics">
        {n.steps != null && (
          <div className="cogs__metric">
            <dt>steps</dt>
            <dd>{n.steps}</dd>
          </div>
        )}
        {n.wall != null && (
          <div className="cogs__metric">
            <dt>wall</dt>
            <dd>{n.wall.toFixed(1)}s</dd>
          </div>
        )}
        {(n.inputTokens != null || n.outputTokens != null) && (
          <div className="cogs__metric">
            <dt>tokens</dt>
            <dd>
              {tokens(n.inputTokens)} in · {tokens(n.outputTokens)} out
            </dd>
          </div>
        )}
        {n.subagentCount != null && n.subagentCount > 0 && (
          <div className="cogs__metric">
            <dt>sub-agents</dt>
            <dd>
              {n.subagentCount}
              {n.subagentFailed ? ` · ${n.subagentFailed} failed` : ""}
            </dd>
          </div>
        )}
      </dl>
    </section>
  );
}
