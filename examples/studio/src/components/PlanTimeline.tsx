import type { FabriEvent } from "../lib/events";
import { EventType } from "../lib/events";

// The manager's decomposed plan, rendered as a live step timeline. fabri emits
// plan_started (the ordered items), then plan_item_started / plan_item_finished
// per step, then plan_finished. We aggregate those into per-step status so the
// run reads as "step 2 of 5 running" instead of a flat narration soup.

type StepState = "pending" | "running" | "done" | "failed";

interface Step {
  index: number;
  goal: string;
  state: StepState;
  reason?: string;
}

function derive(planEvents: FabriEvent[]): { steps: Step[]; done: boolean; completed?: number; total?: number } {
  const byIndex = new Map<number, Step>();
  let order: number[] = [];
  let done = false;
  let completed: number | undefined;
  let total: number | undefined;

  for (const ev of planEvents) {
    switch (ev.type) {
      case EventType.PLAN_STARTED: {
        const items = ev.items ?? [];
        order = ev.order ?? items.map((_, i) => i);
        items.forEach((it, idx) => {
          byIndex.set(idx, {
            index: idx,
            goal: (it.goal ?? `Step ${idx + 1}`) as string,
            state: "pending",
          });
        });
        break;
      }
      case EventType.PLAN_ITEM_STARTED: {
        const step = byIndex.get(ev.index ?? -1);
        if (step) step.state = "running";
        else if (ev.index != null)
          byIndex.set(ev.index, { index: ev.index, goal: ev.goal ?? `Step ${ev.index + 1}`, state: "running" });
        break;
      }
      case EventType.PLAN_ITEM_FINISHED: {
        const step = byIndex.get(ev.index ?? -1);
        if (step) {
          step.state = ev.ok ? "done" : "failed";
          if (!ev.ok) step.reason = ev.reason;
        }
        break;
      }
      case EventType.PLAN_FINISHED: {
        done = true;
        completed = ev.items_completed;
        total = ev.items_total;
        break;
      }
    }
  }

  const seq = order.length ? order : [...byIndex.keys()].sort((a, b) => a - b);
  const steps = seq.map((i) => byIndex.get(i)).filter((s): s is Step => Boolean(s));
  return { steps, done, completed, total };
}

const MARK: Record<StepState, string> = {
  pending: "○",
  running: "◐",
  done: "●",
  failed: "✕",
};

export function PlanTimeline({ planEvents }: { planEvents: FabriEvent[] }) {
  const { steps, done, completed, total } = derive(planEvents);
  if (!steps.length) return null;
  const runningIdx = steps.findIndex((s) => s.state === "running");
  const total_ = total ?? steps.length;
  const heading = done
    ? `Plan complete · ${completed ?? steps.filter((s) => s.state === "done").length}/${total_} steps`
    : runningIdx >= 0
      ? `Plan · step ${runningIdx + 1} of ${total_}`
      : `Plan · ${total_} steps`;

  return (
    <section className="plan" aria-label="run plan">
      <div className="plan__head">{heading}</div>
      <ol className="plan__steps">
        {steps.map((s) => (
          <li key={s.index} className={`plan__step plan__step--${s.state}`}>
            <span className="plan__mark" aria-hidden>
              {MARK[s.state]}
            </span>
            <span className="plan__goal">{s.goal}</span>
            {s.reason && <span className="plan__reason">{s.reason}</span>}
          </li>
        ))}
      </ol>
    </section>
  );
}
