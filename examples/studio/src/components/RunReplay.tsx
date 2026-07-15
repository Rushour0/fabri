import { useEffect, useReducer } from "react";
import type { FabriEvent, RunResult } from "../lib/events";
import { buildTimeline } from "../lib/timeline";
import { Message } from "./Message";
import { CostSummary } from "./CostSummary";

// A read-only view of a past run, opened from history. The run is already
// finished, so its trace drains immediately over the same SSE endpoint the live
// conversation uses — we just don't offer any interaction (no ask/answer, no
// follow-up). Reuses the exact timeline + cost rendering as the live view.

interface State {
  events: FabriEvent[];
  result: RunResult | null;
  loading: boolean;
  error: string | null;
}

type Action =
  | { kind: "event"; event: FabriEvent }
  | { kind: "result"; result: RunResult }
  | { kind: "error"; error: string };

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "event":
      return { ...state, events: [...state.events, action.event] };
    case "result":
      return { ...state, result: action.result, loading: false };
    case "error":
      return { ...state, error: action.error, loading: false };
    default:
      return state;
  }
}

export function RunReplay({ sessionId, onBack }: { sessionId: string; onBack: () => void }) {
  const [state, dispatch] = useReducer(reducer, {
    events: [],
    result: null,
    loading: true,
    error: null,
  });

  useEffect(() => {
    const es = new EventSource(`/runs/${encodeURIComponent(sessionId)}/events`);
    es.onmessage = (m) => {
      try {
        dispatch({ kind: "event", event: JSON.parse(m.data) as FabriEvent });
      } catch {
        /* ignore */
      }
    };
    es.addEventListener("result", (m) => {
      try {
        dispatch({ kind: "result", result: JSON.parse((m as MessageEvent).data) as RunResult });
      } catch {
        /* ignore */
      }
      es.close();
    });
    es.onerror = () => {
      es.close();
      dispatch({ kind: "error", error: "couldn't replay this run" });
    };
    return () => es.close();
  }, [sessionId]);

  const timeline = buildTimeline(state.events);
  const hasCostItem = timeline.some((i) => i.cls === "cost");

  return (
    <div className="replay">
      <div className="replay__bar">
        <button className="btn replay__back" onClick={onBack}>
          ← History
        </button>
        <span className="replay__label">Read-only run</span>
      </div>
      {state.loading && timeline.length === 0 && <div className="history__empty">Loading run…</div>}
      {timeline.map((item) => (
        <Message key={item.key} item={item} />
      ))}
      {!hasCostItem && state.result?.cost && <CostSummary surface={state.result.cost} />}
      {state.error && <div className="error-banner">{state.error}</div>}
    </div>
  );
}
