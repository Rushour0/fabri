import { useEffect, useRef } from "react";
import { useRunEvents } from "./hooks/useRunEvents";
import { buildTimeline, pendingAsks } from "./lib/timeline";
import { Message } from "./components/Message";
import { AskUserCard } from "./components/AskUserCard";
import { Composer } from "./components/Composer";

const STATUS_LABEL: Record<string, string> = {
  idle: "Ready",
  submitting: "Starting…",
  running: "Running",
  done: "Done",
  error: "Error",
};

export default function App() {
  const run = useRunEvents();
  const timeline = buildTimeline(run.events);
  const asks = pendingAsks(run.events, run.answered);
  const busy = run.status === "submitting" || run.status === "running";

  // Keep the conversation pinned to the newest message.
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [timeline.length, asks.length, run.status]);

  const empty = run.status === "idle" && timeline.length === 0;

  return (
    <div className="app">
      <header className="header">
        <div className="header__brand">
          <span className="header__logo" aria-hidden />
          <span className="header__title">Fabri Studio</span>
        </div>
        <div className={`status status--${run.status}`}>
          <span className="status__dot" aria-hidden />
          {STATUS_LABEL[run.status] ?? run.status}
        </div>
      </header>

      <main className="thread">
        {empty && (
          <div className="empty">
            <p className="empty__title">Talk to your fabri agency</p>
            <p className="empty__sub">
              Submit a task below. The manager streams its progress here and asks you
              questions when it needs a decision.
            </p>
          </div>
        )}

        {timeline.map((item) => (
          <Message key={item.key} item={item} />
        ))}

        {asks.map((ev, i) => (
          <AskUserCard
            key={ev.question_id}
            ev={ev}
            onAnswer={run.answer}
            // Answer questions one at a time — answering the first may unblock
            // the next server-side.
            disabled={i > 0}
          />
        ))}

        {run.error && <div className="error-banner">{run.error}</div>}

        <div ref={endRef} />
      </main>

      <footer className="footer">
        {run.status === "done" || run.status === "error" ? (
          <div className="footer__row">
            <span className="footer__done">Run finished.</span>
            <button className="btn" onClick={run.reset}>
              New run
            </button>
          </div>
        ) : (
          <Composer onSubmit={run.start} busy={busy} />
        )}
      </footer>
    </div>
  );
}
