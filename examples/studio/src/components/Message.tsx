import { useState } from "react";
import type { TimelineItem } from "../lib/timeline";
import type { FabriEvent } from "../lib/events";
import { activityLabel, isSubagent } from "../lib/events";
import { Markdown } from "../lib/markdown";

// Renders one timeline entry according to its message class. Hierarchy is carried
// by how much container a class gets — the manager message is the only full card;
// narration/thought/activity recede to bare text, a rail, and a chip.
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

const OK_OUTCOMES = new Set(["success", "success_with_recovery"]);

// The manager's definitive message — the primary, load-bearing channel. Markdown
// so `**bold**`, lists, and code render instead of showing literal syntax.
function ManagerMessage({ ev }: { ev: FabriEvent }) {
  const outcome = ev.outcome;
  return (
    <div className="msg--manager">
      <div className="msg__role">
        <span className="avatar avatar--manager">M</span>
        <span className="msg__name">Manager</span>
      </div>
      <div className="msg__body">
        <Markdown text={ev.text ?? ""} />
      </div>
      {outcome && !OK_OUTCOMES.has(outcome) && (
        <div className="msg__meta">outcome: {outcome}</div>
      )}
    </div>
  );
}

// The narrator's short "what's happening now" line — a bare caption, no chrome.
function NarrationLine({ ev }: { ev: FabriEvent }) {
  return (
    <div className="narration">
      <span className="narration__mark" aria-hidden>
        ›
      </span>
      <span>{ev.text}</span>
    </div>
  );
}

// Main-model reasoning — a left-rail-only disclosure, no card fill.
function ThoughtCard({ ev }: { ev: FabriEvent }) {
  const [open, setOpen] = useState(false);
  const text = (ev.text ?? "").trim();
  const preview = text.length > 88 ? text.slice(0, 88) + "…" : text;
  return (
    <div className={"thought" + (open ? " thought--open" : "")}>
      <button
        className="thought__toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="thought__chevron" aria-hidden>
          ▶
        </span>
        <span className="thought__label">thinking</span>
        {!open && <span className="thought__preview">{preview}</span>}
      </button>
      {open && <div className="thought__full">{text}</div>}
    </div>
  );
}

// Sub-agent / tool activity — the smallest footprint; consecutive rows merge
// into one quiet texture. Kept (unlike ludexel, which hides tool events).
function ActivityChip({ ev }: { ev: FabriEvent }) {
  return (
    <div className={"activity" + (isSubagent(ev) ? " activity--subagent" : "")}>
      <span className="activity__icon" aria-hidden>
        {isSubagent(ev) ? "⤷" : "•"}
      </span>
      <span>{activityLabel(ev)}</span>
    </div>
  );
}

// Cost / COGS — a receipt stapled to the run: quietest tier, right-aligned.
function CostFooter({ ev }: { ev: FabriEvent }) {
  const cost = ev.total_cost_usd ?? ev.cost_usd;
  const steps = ev.step_count;
  const wall = ev.wall_time_s;
  return (
    <div className="cost">
      {cost != null && <span>${Number(cost).toFixed(4)}</span>}
      {steps != null && <span>{steps} steps</span>}
      {wall != null && <span>{Number(wall).toFixed(1)}s</span>}
    </div>
  );
}

// A non-success terminal event — a full-width rule, a different shape from every
// message class so a run-ending failure reads as "the timeline changed state."
function TerminalNote({ ev }: { ev: FabriEvent }) {
  const reason = ev.text ?? ev.reason ?? ev.outcome;
  return (
    <div>
      <div className="terminal">
        <span className="terminal__label">{ev.type}</span>
      </div>
      {reason && <div className="terminal__reason">{reason}</div>}
    </div>
  );
}
