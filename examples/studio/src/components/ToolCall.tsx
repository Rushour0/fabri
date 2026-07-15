import { useState } from "react";
import type { FabriEvent, ToolStatus } from "../lib/events";
import {
  activityLabel,
  EventType,
  isSubagent,
  toolDuration,
  toolStatus,
} from "../lib/events";

// A single tool call, paired from its tool_started + tool_call events. Unlike
// the old one-line chip, this shows the call's name, live status, duration, and
// (on expand) its args and result — the fidelity the trace always carried.
// spawn_subagent and parallel_group_started get their own light treatments.

const STATUS_MARK: Record<ToolStatus, string> = {
  running: "◍",
  ok: "✓",
  error: "✕",
};

function fmtDuration(s: number | null): string | null {
  if (s == null) return null;
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  return `${s.toFixed(s < 10 ? 1 : 0)}s`;
}

function preview(value: unknown, max = 120): string {
  let s: string;
  try {
    s = typeof value === "string" ? value : JSON.stringify(value);
  } catch {
    s = String(value);
  }
  s = (s ?? "").replace(/\s+/g, " ").trim();
  return s.length > max ? s.slice(0, max) + "…" : s;
}

export function ToolCall({ ev, result }: { ev: FabriEvent; result?: FabriEvent }) {
  // The parallel-group header lists its fan-out members; individual members
  // render as their own cards below it.
  if (ev.type === EventType.PARALLEL_GROUP_STARTED) {
    return (
      <div className="tool tool--group">
        <span className="tool__icon" aria-hidden>
          ⇉
        </span>
        <span className="tool__name">{activityLabel(ev)}</span>
        {Array.isArray(ev.calls) && (
          <span className="tool__group-members">{ev.calls.map((c) => c.name).join(", ")}</span>
        )}
      </div>
    );
  }

  const [open, setOpen] = useState(false);
  const status = toolStatus(result);
  const sub = isSubagent(ev);
  const dur = fmtDuration(toolDuration(ev, result));
  const args = ev.args ?? result?.args;
  const resultBody = result?.result;
  const errorMsg = status === "error" ? resultBody?.error ?? "failed" : null;
  const hasDetail = Boolean((args && Object.keys(args).length) || resultBody);

  return (
    <div className={`tool tool--${status}` + (sub ? " tool--subagent" : "")}>
      <button
        className="tool__head"
        onClick={() => hasDetail && setOpen((v) => !v)}
        aria-expanded={hasDetail ? open : undefined}
        disabled={!hasDetail}
      >
        <span className={`tool__icon tool__icon--${status}`} aria-hidden>
          {sub ? "⤷" : STATUS_MARK[status]}
        </span>
        <span className="tool__name">{activityLabel(ev)}</span>
        {status === "running" && <span className="tool__state">running…</span>}
        {errorMsg && <span className="tool__error">{preview(errorMsg, 80)}</span>}
        {dur && <span className="tool__dur">{dur}</span>}
        {hasDetail && (
          <span className="tool__chevron" aria-hidden>
            {open ? "▾" : "▸"}
          </span>
        )}
      </button>
      {open && (
        <div className="tool__detail">
          {args && Object.keys(args).length > 0 && (
            <div className="tool__field">
              <span className="tool__field-label">args</span>
              <pre className="tool__code">{JSON.stringify(args, null, 2)}</pre>
            </div>
          )}
          {resultBody != null && (
            <div className="tool__field">
              <span className="tool__field-label">result</span>
              <pre className="tool__code">{JSON.stringify(resultBody, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
