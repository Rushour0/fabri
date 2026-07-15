import { useState } from "react";
import type { TimelineItem } from "../lib/timeline";
import type { FabriEvent } from "../lib/events";
import { activityLabel, isSubagent } from "../lib/events";

// Renders one timeline entry according to its message class. Each class is
// visually distinct — that distinction is the whole point of the taxonomy.
export function Message({ item }: { item: TimelineItem }) {
  switch (item.cls) {
    case "manager":
      return <ManagerMessage ev={item.ev} />;
    case "narration":
      return <NarrationLine ev={item.ev} />;
    case "thought":
      return <ThoughtCard ev={item.ev} />;
    case "activity":
      return <ActivityChip ev={item.ev} />;
    case "cost":
      return <CostFooter ev={item.ev} />;
    case "terminal":
      return <TerminalNote ev={item.ev} />;
    default:
      return null;
  }
}

// The manager's definitive message — the primary, load-bearing channel.
function ManagerMessage({ ev }: { ev: FabriEvent }) {
  const outcome = ev.outcome;
  return (
    <div className="msg msg--manager">
      <div className="msg__role">
        <span className="avatar avatar--manager">M</span>
        <span className="msg__name">Manager</span>
      </div>
      <div className="msg__body">{ev.text}</div>
      {outcome && outcome !== "success" && (
        <div className="msg__meta">outcome: {outcome}</div>
      )}
    </div>
  );
}

// The narrator's short "what's happening now" line — secondary, light.
function NarrationLine({ ev }: { ev: FabriEvent }) {
  return (
    <div className="narration">
      <span className="narration__dot" aria-hidden />
      <span className="narration__text">{ev.text}</span>
    </div>
  );
}

// Main-model reasoning — collapsible, low-emphasis.
function ThoughtCard({ ev }: { ev: FabriEvent }) {
  const [open, setOpen] = useState(false);
  const text = (ev.text ?? "").trim();
  const preview = text.length > 90 ? text.slice(0, 90) + "…" : text;
  return (
    <div className="thought">
      <button className="thought__toggle" onClick={() => setOpen((v) => !v)}>
        <span className="thought__chevron">{open ? "▾" : "▸"}</span>
        <span className="thought__label">thinking</span>
        {!open && <span className="thought__preview">{preview}</span>}
      </button>
      {open && <div className="thought__full">{text}</div>}
    </div>
  );
}

// Sub-agent / tool activity — a light chip. Kept (unlike ludexel, which hides
// tool events), because seeing sub-agents run is part of the ask.
function ActivityChip({ ev }: { ev: FabriEvent }) {
  return (
    <div className={"activity" + (isSubagent(ev) ? " activity--subagent" : "")}>
      <span className="activity__icon" aria-hidden>
        {isSubagent(ev) ? "⤷" : "⚙"}
      </span>
      <span className="activity__label">{activityLabel(ev)}</span>
    </div>
  );
}

// Cost / COGS footer from the run's usage event.
function CostFooter({ ev }: { ev: FabriEvent }) {
  const cost = ev.total_cost_usd ?? ev.cost_usd;
  const steps = ev.step_count;
  const wall = ev.wall_time_s;
  return (
    <div className="cost">
      {cost != null && <span className="cost__item">${Number(cost).toFixed(4)}</span>}
      {steps != null && <span className="cost__item">{steps} steps</span>}
      {wall != null && <span className="cost__item">{Number(wall).toFixed(1)}s</span>}
    </div>
  );
}

// A non-success terminal event (failed / incomplete / error).
function TerminalNote({ ev }: { ev: FabriEvent }) {
  return (
    <div className="terminal">
      <span className="terminal__badge">{ev.type}</span>
      <span className="terminal__reason">{ev.text ?? ev.reason ?? ev.outcome}</span>
    </div>
  );
}
