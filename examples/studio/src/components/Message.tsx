import { useState } from "react";
import type { TimelineItem } from "../lib/timeline";
import type { FabriEvent } from "../lib/events";
import { EventType } from "../lib/events";
import { Markdown } from "../lib/markdown";
import { PlanTimeline } from "./PlanTimeline";
import { ToolCall } from "./ToolCall";
import { CostSummary } from "./CostSummary";

// Renders one timeline entry according to its message class. Hierarchy is carried
// by how much container a class gets — the manager message is the only full card;
// narration/thought recede to bare text and a rail; tool/plan/cost are structured
// but quiet; warnings break the texture because they flag risk to spend or truth.
export function Message({ item, maxCostUsd }: { item: TimelineItem; maxCostUsd?: number }) {
  switch (item.cls) {
    case "manager":
      return <ManagerMessage ev={item.ev} />;
    case "narration":
      return <NarrationLine ev={item.ev} />;
    case "thought":
      return <ThoughtCard ev={item.ev} />;
    case "plan":
      return <PlanTimeline planEvents={item.planEvents ?? [item.ev]} />;
    case "tool":
      return <ToolCall ev={item.ev} result={item.toolResult} />;
    case "cost":
      return <CostSummary event={item.ev} maxCostUsd={maxCostUsd} />;
    case "warning":
      return <WarningNote ev={item.ev} />;
    case "note":
      return <ValidationNote ev={item.ev} />;
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

// cost_unaccounted / discrepancy — a flagged risk that the run's COGS is
// under-reported or that a claimed write never landed. Deliberately breaks the
// quiet texture: these are exactly what a debugging UI must not hide.
function WarningNote({ ev }: { ev: FabriEvent }) {
  const isCost = ev.type === EventType.COST_UNACCOUNTED;
  const label = isCost ? "unaccounted cost" : "discrepancy";
  const detail = isCost
    ? `${ev.tool ?? "sub-agent"}: ${ev.reason ?? "spend not attributed"}`
    : `${ev.path ?? "artifact"}: ${ev.reason ?? "claimed but not verified"}`;
  return (
    <div className="warning" role="note">
      <span className="warning__icon" aria-hidden>
        ⚠
      </span>
      <span className="warning__label">{label}</span>
      <span className="warning__detail">{detail}</span>
    </div>
  );
}

// structured_output that failed validation — a quiet note the run retried a
// typed answer, so a schema-retry isn't invisible.
function ValidationNote({ ev }: { ev: FabriEvent }) {
  const attempt = ev.attempt != null ? ev.attempt + 1 : undefined;
  const first = Array.isArray(ev.errors) ? ev.errors[0] : undefined;
  return (
    <div className="note">
      <span className="note__mark" aria-hidden>
        ↻
      </span>
      <span>
        output validation failed{attempt ? ` (attempt ${attempt})` : ""}
        {first ? ` — ${first}` : ""}
      </span>
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
