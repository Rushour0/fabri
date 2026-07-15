// Thin wrappers over the `fabri serve` HTTP API (src/fabri/service/http_server.py).
// All paths are same-origin — the Vite dev server proxies them to `fabri serve`.
import type { RunResult } from "./events";

export interface SubmitResponse {
  session_id: string;
  status: string;
}

export async function submitRun(task: string): Promise<SubmitResponse> {
  const res = await fetch("/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `submit failed (${res.status})`);
  }
  return res.json();
}

// Answer a mid-run `ask_user` question. The endpoint is added by the HITL
// backend slice; correlation is by `question_id`.
export async function answerAsk(
  sessionId: string,
  questionId: string,
  answer: string,
  selectedOption?: string,
): Promise<void> {
  const res = await fetch(`/runs/${encodeURIComponent(sessionId)}/answer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      question_id: questionId,
      answer,
      ...(selectedOption !== undefined ? { selected_option: selectedOption } : {}),
    }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.error ?? `answer failed (${res.status})`);
  }
}

export type { RunResult };
