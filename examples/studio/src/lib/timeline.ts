import type { FabriEvent, MessageClass } from "./events";
import { classify, EventType } from "./events";

// One renderable entry in the conversation. The raw event is carried through so
// components can pull class-specific fields.
export interface TimelineItem {
  key: string;
  cls: MessageClass;
  ev: FabriEvent;
}

// Turn the raw, append-only event stream into the ordered list the UI renders.
// Events keep their arrival order (fabri writes the trace in causal order, and
// we never reorder); we only DROP plumbing events and DEDUPE repeated reasoning.
export function buildTimeline(events: FabriEvent[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  const seenThoughts = new Set<string>();

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
