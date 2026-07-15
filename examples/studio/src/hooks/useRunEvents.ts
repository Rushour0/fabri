import { useCallback, useReducer, useRef } from "react";
import { submitRun, answerAsk, cancelRun, getResult } from "../lib/api";
import type { FabriEvent, RunResult } from "../lib/events";
import { EventType } from "../lib/events";

// A thread of runs. Studio started as one run per submit; it now keeps an
// ordered list of turns under one thread_id so a conversation carries across
// submits. Each turn is still one run streamed over one EventSource, ending on
// fabri's terminal `event: result` frame (not stream EOF — EventSource would
// otherwise auto-reconnect forever). Turns are sequential: the composer is
// disabled while the active (last) turn is in flight, so only the last turn is
// ever live.

export type RunStatus = "idle" | "submitting" | "running" | "done" | "error" | "cancelled";

export interface Turn {
  sessionId: string | null;
  task: string;
  events: FabriEvent[];
  result: RunResult | null;
  status: RunStatus;
  answered: Set<string>;
  terminal: boolean;
}

interface State {
  threadId: string | null;
  turns: Turn[];
  error: string | null;
}

const SUCCESS_OUTCOMES = new Set(["success", "success_with_recovery"]);

type Action =
  | { kind: "submit"; task: string; threadId: string }
  | { kind: "started"; sessionId: string }
  | { kind: "event"; event: FabriEvent }
  | { kind: "result"; result: RunResult }
  | { kind: "answered"; questionId: string }
  | { kind: "cancelled" }
  | { kind: "error"; error: string }
  | { kind: "reset" };

const initialState: State = { threadId: null, turns: [], error: null };

function newTurn(task: string): Turn {
  return {
    sessionId: null,
    task,
    events: [],
    result: null,
    status: "submitting",
    answered: new Set(),
    terminal: false,
  };
}

// Apply a patch to the active (last) turn, returning a new turns array.
function patchActive(turns: Turn[], patch: (t: Turn) => Turn): Turn[] {
  if (turns.length === 0) return turns;
  const next = turns.slice();
  next[next.length - 1] = patch(next[next.length - 1]);
  return next;
}

function reducer(state: State, action: Action): State {
  switch (action.kind) {
    case "submit":
      return {
        threadId: action.threadId,
        turns: [...state.turns, newTurn(action.task)],
        error: null,
      };
    case "started":
      return {
        ...state,
        turns: patchActive(state.turns, (t) => ({ ...t, status: "running", sessionId: action.sessionId })),
      };
    case "event": {
      const ev = action.event;
      return {
        ...state,
        turns: patchActive(state.turns, (t) => {
          const turn = { ...t, events: [...t.events, ev] };
          // The run's own terminal event is the authoritative verdict — more
          // reliable than the result envelope (parsed from subprocess stdout).
          if (ev.type === EventType.FINAL) {
            turn.status = ev.outcome && !SUCCESS_OUTCOMES.has(ev.outcome) ? "error" : "done";
            turn.terminal = true;
          } else if (ev.type === EventType.FAILED || ev.type === EventType.INCOMPLETE) {
            turn.status = "error";
            turn.terminal = true;
          }
          return turn;
        }),
      };
    }
    case "result":
      return {
        ...state,
        turns: patchActive(state.turns, (t) => {
          // Supplementary (carries the cost surface). Only decides status if no
          // terminal event set it — otherwise the run's own outcome wins.
          if (t.terminal) return { ...t, result: action.result };
          const failed = action.result.success === false;
          return { ...t, result: action.result, status: failed ? "error" : "done" };
        }),
      };
    case "answered":
      return {
        ...state,
        turns: patchActive(state.turns, (t) => {
          const answered = new Set(t.answered);
          answered.add(action.questionId);
          return { ...t, answered };
        }),
      };
    case "cancelled":
      return {
        ...state,
        turns: patchActive(state.turns, (t) =>
          t.terminal ? t : { ...t, status: "cancelled", terminal: true },
        ),
      };
    case "error":
      return {
        ...state,
        error: action.error,
        turns: patchActive(state.turns, (t) => ({ ...t, status: "error" })),
      };
    case "reset":
      return initialState;
    default:
      return state;
  }
}

// Compact transcript preamble so a follow-up turn has conversational continuity.
// fabri's memory carries facts/guidelines across turns (we also bind a shared
// memory collection per thread), but raw "what did you just say" continuity is
// most reliable when the prior exchange is restated in the task itself.
function threadPreamble(turns: Turn[]): string {
  const parts: string[] = [];
  turns.forEach((t, i) => {
    const answer = t.result?.final_text ?? lastManagerText(t.events);
    if (!answer) return;
    parts.push(`Turn ${i + 1}:\nUser: ${t.task}\nYou: ${answer.trim()}`);
  });
  if (!parts.length) return "";
  return `Earlier in this conversation:\n\n${parts.join("\n\n")}\n\n---\n\nNow the user says:\n`;
}

function lastManagerText(events: FabriEvent[]): string | undefined {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].type === EventType.FINAL && (events[i].text ?? "").trim()) return events[i].text;
  }
  return undefined;
}

function genThreadId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `thread-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
}

export interface UseRunEvents {
  threadId: string | null;
  turns: Turn[];
  status: RunStatus;
  error: string | null;
  start: (task: string) => Promise<void>;
  answer: (questionId: string, answer: string, selectedOption?: string) => Promise<void>;
  cancel: () => Promise<void>;
  reset: () => void;
}

export function useRunEvents(): UseRunEvents {
  const [state, dispatch] = useReducer(reducer, initialState);
  const esRef = useRef<EventSource | null>(null);
  const sessionRef = useRef<string | null>(null);
  // Latest state, readable inside callbacks without stale-closure churn.
  const stateRef = useRef(state);
  stateRef.current = state;

  const closeStream = useCallback(() => {
    esRef.current?.close();
    esRef.current = null;
  }, []);

  const openStream = useCallback(
    (sessionId: string) => {
      const es = new EventSource(`/runs/${encodeURIComponent(sessionId)}/events`);
      esRef.current = es;

      es.onmessage = (m) => {
        try {
          dispatch({ kind: "event", event: JSON.parse(m.data) as FabriEvent });
        } catch {
          /* ignore malformed SSE frame */
        }
      };
      es.addEventListener("result", (m) => {
        try {
          dispatch({ kind: "result", result: JSON.parse((m as MessageEvent).data) as RunResult });
        } catch {
          /* ignore */
        }
        closeStream();
      });
      es.onerror = async () => {
        if (sessionRef.current !== sessionId) return;
        closeStream();
        try {
          const res = await getResult(sessionId);
          dispatch({ kind: "result", result: res });
          return;
        } catch {
          /* fall through */
        }
        dispatch({ kind: "error", error: "stream disconnected before the run finished" });
      };
    },
    [closeStream],
  );

  const start = useCallback(
    async (task: string) => {
      closeStream();
      const prior = stateRef.current;
      const threadId = prior.threadId ?? genThreadId();
      const isFollowup = prior.turns.length > 0;
      dispatch({ kind: "submit", task, threadId });

      // Follow-ups share a memory collection (fabri carries facts across turns)
      // and get a transcript preamble for raw conversational continuity.
      const overrides = { memory: { collection: threadId } };
      const effectiveTask = isFollowup ? threadPreamble(prior.turns) + task : task;

      let sessionId: string;
      try {
        const res = await submitRun(effectiveTask, { threadId, overrides });
        sessionId = res.session_id;
      } catch (e) {
        dispatch({ kind: "error", error: e instanceof Error ? e.message : String(e) });
        return;
      }
      sessionRef.current = sessionId;
      dispatch({ kind: "started", sessionId });
      openStream(sessionId);
    },
    [closeStream, openStream],
  );

  const answer = useCallback(async (questionId: string, answerText: string, selectedOption?: string) => {
    const sessionId = sessionRef.current;
    if (!sessionId) return;
    await answerAsk(sessionId, questionId, answerText, selectedOption);
    dispatch({ kind: "answered", questionId });
  }, []);

  const cancel = useCallback(async () => {
    const sessionId = sessionRef.current;
    if (!sessionId) return;
    closeStream();
    try {
      await cancelRun(sessionId);
    } catch {
      /* the run may have already ended; treat as cancelled locally */
    }
    dispatch({ kind: "cancelled" });
  }, [closeStream]);

  const reset = useCallback(() => {
    closeStream();
    sessionRef.current = null;
    dispatch({ kind: "reset" });
  }, [closeStream]);

  const active = state.turns[state.turns.length - 1];
  const status: RunStatus = active ? active.status : "idle";

  return { threadId: state.threadId, turns: state.turns, status, error: state.error, start, answer, cancel, reset };
}

export { EventType };
