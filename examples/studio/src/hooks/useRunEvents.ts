import { useCallback, useReducer, useRef } from "react";
import { submitRun, answerAsk } from "../lib/api";
import type { FabriEvent, RunResult } from "../lib/events";
import { EventType } from "../lib/events";

// One run, streamed over one EventSource. We deliberately drop ludexel's
// multi-run store / persisted-log replay: Studio starts a fresh run per submit
// and treats SSE as a live tail, ending on fabri's terminal `event: result`
// frame (not stream EOF — EventSource would otherwise auto-reconnect forever).

export type RunStatus = "idle" | "submitting" | "running" | "done" | "error";

interface State {
  status: RunStatus;
  sessionId: string | null;
  task: string | null;
  events: FabriEvent[];
  result: RunResult | null;
  error: string | null;
  // question_ids the user has already answered (drives AskUserCard dismissal).
  answered: Set<string>;
  // True once a terminal event (final/failed/incomplete) has set the outcome —
  // so the trailing `result` frame doesn't override the run's own verdict.
  terminal: boolean;
}

const SUCCESS_OUTCOMES = new Set(["success", "success_with_recovery"]);

type Action =
  | { kind: "submit"; task: string }
  | { kind: "started"; sessionId: string }
  | { kind: "event"; event: FabriEvent }
  | { kind: "result"; result: RunResult }
  | { kind: "answered"; questionId: string }
  | { kind: "error"; error: string }
  | { kind: "reset" };

const initialState: State = {
  status: "idle",
  sessionId: null,
  task: null,
  events: [],
  result: null,
  error: null,
  answered: new Set(),
  terminal: false,
};

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "submit":
      return { ...initialState, status: "submitting", task: action.task, answered: new Set() };
    case "started":
      return { ...state, status: "running", sessionId: action.sessionId };
    case "event": {
      const ev = action.event;
      const next = { ...state, events: [...state.events, ev] };
      // The run's own terminal event is the authoritative verdict — more reliable
      // than the result envelope (which is parsed from subprocess stdout).
      if (ev.type === "final") {
        next.status = ev.outcome && !SUCCESS_OUTCOMES.has(ev.outcome) ? "error" : "done";
        next.terminal = true;
      } else if (ev.type === "failed" || ev.type === "incomplete") {
        next.status = "error";
        next.terminal = true;
      }
      return next;
    }
    case "result": {
      // Supplementary (carries the cost surface). Only decides status if no
      // terminal event set it — otherwise the run's own outcome wins.
      if (state.terminal) return { ...state, result: action.result };
      const failed = action.result.success === false;
      return { ...state, status: failed ? "error" : "done", result: action.result };
    }
    case "answered": {
      const answered = new Set(state.answered);
      answered.add(action.questionId);
      return { ...state, answered };
    }
    case "error":
      return { ...state, status: "error", error: action.error };
    case "reset":
      return initialState;
    default:
      return state;
  }
}

export interface UseRunEvents extends State {
  start: (task: string) => Promise<void>;
  answer: (questionId: string, answer: string, selectedOption?: string) => Promise<void>;
  reset: () => void;
}

export function useRunEvents(): UseRunEvents {
  const [state, dispatch] = useReducer(reducer, initialState);
  const esRef = useRef<EventSource | null>(null);
  const sessionRef = useRef<string | null>(null);

  const closeStream = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
  }, []);

  const start = useCallback(
    async (task: string) => {
      closeStream();
      dispatch({ kind: "submit", task });
      let sessionId: string;
      try {
        const res = await submitRun(task);
        sessionId = res.session_id;
      } catch (e) {
        dispatch({ kind: "error", error: e instanceof Error ? e.message : String(e) });
        return;
      }
      sessionRef.current = sessionId;
      dispatch({ kind: "started", sessionId });

      const es = new EventSource(`/runs/${encodeURIComponent(sessionId)}/events`);
      esRef.current = es;

      // Unnamed frames = live fabri events. Swallow a malformed frame rather
      // than letting one bad line kill the stream (ludexel sse.ts pattern).
      es.onmessage = (m) => {
        try {
          dispatch({ kind: "event", event: JSON.parse(m.data) as FabriEvent });
        } catch {
          /* ignore malformed SSE frame */
        }
      };

      // Terminal frame: the run's result envelope. Close the stream so the
      // browser doesn't auto-reconnect.
      es.addEventListener("result", (m) => {
        try {
          dispatch({ kind: "result", result: JSON.parse((m as MessageEvent).data) as RunResult });
        } catch {
          /* ignore */
        }
        closeStream();
      });

      // Connection dropped before the terminal frame (run crash, server exit).
      // Try one blocking /result fetch to recover the envelope, else surface it.
      es.onerror = async () => {
        if (sessionRef.current !== sessionId) return;
        closeStream();
        try {
          const res = await fetch(`/runs/${encodeURIComponent(sessionId)}/result`);
          if (res.ok) {
            dispatch({ kind: "result", result: (await res.json()) as RunResult });
            return;
          }
        } catch {
          /* fall through to error */
        }
        dispatch({ kind: "error", error: "stream disconnected before the run finished" });
      };
    },
    [closeStream],
  );

  const answer = useCallback(
    async (questionId: string, answerText: string, selectedOption?: string) => {
      const sessionId = sessionRef.current;
      if (!sessionId) return;
      await answerAsk(sessionId, questionId, answerText, selectedOption);
      dispatch({ kind: "answered", questionId });
    },
    [],
  );

  const reset = useCallback(() => {
    closeStream();
    sessionRef.current = null;
    dispatch({ kind: "reset" });
  }, [closeStream]);

  return { ...state, start, answer, reset };
}

// Re-export for callers that build derived views over the raw stream.
export { EventType };
