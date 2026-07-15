import type { FabriEvent, MessageClass } from "./events";
import { classify, EventType } from "./events";

// One renderable entry in the conversation. The raw event is carried through so
// components can pull class-specific fields. Two classes enrich the item beyond
// its primary event:
//   - "tool": `toolResult` is the paired `tool_call` (result/status/duration)
//     for a `tool_started`, so one card shows the whole call rather than two rows.
//   - "plan":  `planEvents` is every plan_* event, aggregated into one evolving
//     step timeline instead of a row per plan transition.
export interface TimelineItem {
  key: string;
  cls: MessageClass;
  ev: FabriEvent;
  toolResult?: FabriEvent;
  planEvents?: FabriEvent[];
}

// Turn the raw, append-only event stream into the ordered list the UI renders.
// Events keep their arrival order (fabri writes the trace in causal order, and
// we never reorder); we DROP plumbing events, DEDUPE repeated reasoning, PAIR
// each tool_started with its tool_call, and FOLD plan_* into one timeline item.
export function buildTimeline(events: FabriEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenThoughts = new Set<string>();
  // `${step}:${call_index}` → the tool item awaiting its paired tool_call.
  const toolIndex = new Map<string, TimelineItem>();
  // The single aggregating plan item (the run has at most one plan today).
  let planItem: TimelineItem | null = null;

  events.forEach((ev, i) => {
    const cls = classify(ev);
    if (!cls) return;

    // Dedupe identical reasoning lines — the same thought can surface more than
    // once across steps and adds only noise (ludexel RunBlock dedupe pattern).
    if (cls === "thought") {
      const text = (ev.text ?? "").trim();
      if (!text || seenThoughts.has(text)) return;
      seenThoughts.add(text);
    }

    // Drop empty narration/final rather than render a blank bubble.
    if ((cls === "narration" || cls === "manager") && !(ev.text ?? "").trim()) return;

    // Fold every plan_* event into one evolving timeline item.
    if (cls === "plan") {
      if (!planItem) {
        planItem = { key: `${i}-plan`, cls, ev, planEvents: [ev] };
        items.push(planItem);
      } else {
        planItem.planEvents!.push(ev);
      }
      return;
    }

    // Pair tool_started ↔ tool_call so a call is one card, not two rows.
    if (cls === "tool" && ev.type !== EventType.PARALLEL_GROUP_STARTED) {
      const pairKey = `${ev.step ?? "?"}:${ev.call_index ?? "?"}`;
      if (ev.type === EventType.TOOL_STARTED) {
        const item: TimelineItem = { key: `${i}-tool`, cls, ev };
        toolIndex.set(pairKey, item);
        items.push(item);
        return;
      }
      // tool_call: attach the result to its started card if we have one.
      const existing = toolIndex.get(pairKey);
      if (existing) {
        existing.toolResult = ev;
        toolIndex.delete(pairKey);
        return;
      }
      // No prior tool_started (defensive): render the call on its own.
      items.push({ key: `${i}-tool`, cls, ev, toolResult: ev });
      return;
    }

    items.push({ key: `${i}-${ev.type}`, cls, ev });
  });

  return items;
}

// Pending questions = ask_user events whose question_id hasn't been answered yet.
export function pendingAsks(events: FabriEvent[], answered: Set<string>): FabriEvent[] {
  const seen = new Set<string>();
  const out: FabriEvent[] = [];
  for (const ev of events) {
    if (ev.type !== EventType.ASK_USER) continue;
    const qid = ev.question_id;
    if (!qid || seen.has(qid) || answered.has(qid)) continue;
    seen.add(qid);
    out.push(ev);
  }
  return out;
}
