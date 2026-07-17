import { useEffect, useRef, useState } from "react";
import { useRunEvents, type Turn } from "./hooks/useRunEvents";
import { buildTimeline, pendingAsks } from "./lib/timeline";
import { Message } from "./components/Message";
import { AskUserCard } from "./components/AskUserCard";
import { Composer } from "./components/Composer";
import { CostSummary } from "./components/CostSummary";
import { HistoryList } from "./components/HistoryList";
import { RunReplay } from "./components/RunReplay";
import { FleetView } from "./components/FleetView";
import { AgencyGraph } from "./components/AgencyGraph";

const STATUS_LABEL: Record<string, string> = {
  idle: "Ready",
  submitting: "Starting…",
  running: "Running",
  done: "Done",
  error: "Error",
  cancelled: "Cancelled",
};

type Surface = "conversation" | "company" | "history" | "fleet" | "replay";

// One turn of the thread: the user's task, then the agent's streamed timeline,
// then that turn's pending questions and cost fallback. Only the active (last)
// turn is interactive.
function TurnBlock({
  turn,
  active,
  onAnswer,
}: {
  turn: Turn;
  active: boolean;
  onAnswer: (q: string, a: string, sel?: string) => Promise<void>;
}) {
  const timeline = buildTimeline(turn.events);
  const asks = active ? pendingAsks(turn.events, turn.answered) : [];
  const hasCostItem = timeline.some((i) => i.cls === "cost");
  const finished = turn.terminal || turn.status === "done" || turn.status === "error";

  return (
    <div className="turn">
      <div className="turn__task">
        <span className="turn__task-bubble">{turn.task}</span>
      </div>
      {timeline.map((item) => (
        <Message key={item.key} item={item} />
      ))}
      {asks.map((ev, i) => (
        <AskUserCard
          key={ev.question_id}
          ev={ev}
          onAnswer={onAnswer}
          // Answer questions one at a time — answering the first may unblock the
          // next server-side.
          disabled={i > 0}
        />
      ))}
      {finished && !hasCostItem && turn.result?.cost && <CostSummary surface={turn.result.cost} />}
    </div>
  );
}

export default function App() {
  const run = useRunEvents();
  const [surface, setSurface] = useState<Surface>("conversation");
  const [replayId, setReplayId] = useState<string | null>(null);
  // Where the replay was opened from, so its back button returns there.
  const [replayFrom, setReplayFrom] = useState<Surface>("history");
  const busy = run.status === "submitting" || run.status === "running";
  const hasThread = run.turns.length > 0;
  const activeIdx = run.turns.length - 1;

  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (surface !== "conversation") return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [run.turns, run.status, surface]);

  const openReplay = (sessionId: string, from: Surface) => {
    setReplayId(sessionId);
    setReplayFrom(from);
    setSurface("replay");
  };

  const empty = surface === "conversation" && !hasThread && run.status === "idle";

  return (
    <div className="app">
      <header className="header">
        <div className="header__brand">
          <span className="header__logo" aria-hidden />
          <span className="header__title">Fabri Studio</span>
        </div>
        <div className="header__right">
          <nav className="tabs" aria-label="views">
            <button
              className={"tab" + (surface === "conversation" ? " tab--on" : "")}
              onClick={() => setSurface("conversation")}
            >
              Conversation
            </button>
            <button
              className={"tab" + (surface === "company" ? " tab--on" : "")}
              onClick={() => setSurface("company")}
            >
              Company
            </button>
            <button
              className={"tab" + (surface === "fleet" ? " tab--on" : "")}
              onClick={() => setSurface("fleet")}
            >
              Fleet
            </button>
            <button
              className={"tab" + (surface === "history" || surface === "replay" ? " tab--on" : "")}
              onClick={() => setSurface("history")}
            >
              History
            </button>
          </nav>
          <div className={`status status--${run.status}`}>
            <span className="status__dot" aria-hidden />
            {STATUS_LABEL[run.status] ?? run.status}
          </div>
        </div>
      </header>

      <main className="thread">
        {surface === "history" && <HistoryList onOpen={(id) => openReplay(id, "history")} />}
        {surface === "fleet" && <FleetView onOpenRun={(id) => openReplay(id, "fleet")} />}
        {surface === "replay" && replayId && (
          <RunReplay key={replayId} sessionId={replayId} onBack={() => setSurface(replayFrom)} />
        )}
        {surface === "company" && (
          hasThread ? <AgencyGraph events={run.turns[activeIdx]?.events ?? []} /> : (
            <div className="agency-empty">Start a task in Conversation to watch your agency's agents work together.</div>
          )
        )}

        {surface === "conversation" && (
          <>
            {empty && (
              <div className="empty">
                <p className="empty__title">Talk to your fabri agency</p>
                <p className="empty__sub">
                  Submit a task below. The manager streams its plan, tool calls, and cost here — and
                  asks you questions when it needs a decision. Follow-ups continue the same thread.
                </p>
              </div>
            )}

            {run.turns.map((turn, i) => (
              <TurnBlock
                key={turn.sessionId ?? `turn-${i}`}
                turn={turn}
                active={i === activeIdx}
                onAnswer={run.answer}
              />
            ))}

            {run.error && <div className="error-banner">{run.error}</div>}
            <div ref={endRef} />
          </>
        )}
      </main>

      {surface === "conversation" && (
        <footer className="footer">
          <div className="footer__stack">
            {hasThread && !busy && (
              <button className="footer__new" onClick={run.reset}>
                New thread
              </button>
            )}
            <Composer onSubmit={run.start} onCancel={run.cancel} busy={busy} followup={hasThread} />
          </div>
        </footer>
      )}
    </div>
  );
}
