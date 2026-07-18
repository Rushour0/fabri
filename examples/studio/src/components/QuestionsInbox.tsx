import { useCallback, useEffect, useRef, useState } from "react";
import { AskUserCard } from "./AskUserCard";
import { answerAsk, listQuestions, type PendingQuestion } from "../lib/api";
import type { FabriEvent } from "../lib/events";

// The cross-run human-in-the-loop surface. Every ask_user question that is
// currently waiting on a human — across every in-flight agency — collects here,
// answerable inline. It reuses AskUserCard (the same card the conversation shows)
// so the answer interaction is identical; the header slot names the source run
// and how long it has been waiting.

const POLL_MS = 3000;
const URGENT_AFTER_S = 300; // 5 min waiting → gently emphasize (amber, never alarm-red)

function waitingLabel(askedTs: number, now: number): string {
  const secs = Math.max(0, Math.floor(now - askedTs));
  if (secs < 45) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 1) return "waiting <1m";
  if (mins < 60) return `waiting ${mins}m`;
  const hrs = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `waiting ${hrs}h ${rem}m` : `waiting ${hrs}h`;
}

export function QuestionsInbox({ onOpenRun }: { onOpenRun?: (sessionId: string) => void }) {
  const [questions, setQuestions] = useState<PendingQuestion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(() => Date.now() / 1000);
  // Questions we just answered — hide them until the server's next poll drops
  // them, so the card doesn't flicker back between the answer and the refetch.
  const answered = useRef<Set<string>>(new Set());

  const refresh = useCallback(async () => {
    try {
      const qs = await listQuestions();
      setQuestions(qs.filter((q) => !answered.current.has(q.question_id)));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const poll = setInterval(refresh, POLL_MS);
    const tick = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => {
      clearInterval(poll);
      clearInterval(tick);
    };
  }, [refresh]);

  const handleAnswer = useCallback(
    async (q: PendingQuestion, answer: string, selectedOption?: string) => {
      await answerAsk(q.session_id, q.question_id, answer, selectedOption);
      answered.current.add(q.question_id);
      setQuestions((prev) => (prev ? prev.filter((x) => x.question_id !== q.question_id) : prev));
      refresh();
    },
    [refresh],
  );

  if (questions === null && error === null) {
    return <div className="qinbox__state">Loading questions…</div>;
  }

  const count = questions?.length ?? 0;

  return (
    <section className="qinbox" aria-label="Questions inbox">
      <div className="qinbox__head">
        <h2 className="qinbox__title">Questions</h2>
        {count > 0 && <span className="qinbox__count">{count} waiting</span>}
      </div>
      <p className="qinbox__sub">
        When an agent needs a decision it can’t make, it waits here for a human. Answer and its run resumes.
      </p>

      {error && <div className="error-banner">Couldn’t load questions: {error}</div>}

      {count === 0 && !error ? (
        <div className="qinbox__empty">
          <span className="qinbox__empty-mark" aria-hidden />
          <p className="qinbox__empty-title">No questions waiting</p>
          <p className="qinbox__empty-sub">
            Your agents are running on their own. They’ll ask here when they need you.
          </p>
        </div>
      ) : (
        <div className="qinbox__list">
          {questions!.map((q) => {
            const urgent = now - q.asked_ts >= URGENT_AFTER_S;
            const source = q.task || q.label || `run ${q.session_id.slice(0, 8)}`;
            const ev = {
              question_id: q.question_id,
              question: q.question,
              options: q.options,
              default: q.default,
            } as FabriEvent;
            return (
              <AskUserCard
                key={q.question_id}
                ev={ev}
                onAnswer={(_qid, answer, selectedOption) => handleAnswer(q, answer, selectedOption)}
                header={
                  <div className="ask__source">
                    <button
                      type="button"
                      className="ask__source-agency"
                      onClick={() => onOpenRun?.(q.session_id)}
                      title="Open this run"
                    >
                      {source}
                    </button>
                    <span
                      className={"ask__source-age" + (urgent ? " ask__source-age--urgent" : "")}
                    >
                      {waitingLabel(q.asked_ts, now)}
                    </span>
                  </div>
                }
              />
            );
          })}
        </div>
      )}
    </section>
  );
}
