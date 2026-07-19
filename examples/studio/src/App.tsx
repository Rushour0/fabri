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
import { QuestionsInbox } from "./components/QuestionsInbox";
import { CompanyOrgChart } from "./components/CompanyOrgChart";
import { CatalogView, type CatalogSelection } from "./components/CatalogView";
import { useHashRoute, type Surface } from "./hooks/useHashRoute";
import { listQuestions, getCompany, getCatalog, type Company, type Catalog } from "./lib/api";

const STATUS_LABEL: Record<string, string> = {
  idle: "Ready",
  submitting: "Starting…",
  running: "Running",
  done: "Done",
  error: "Error",
  cancelled: "Cancelled",
};

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
  // Tab state lives in the URL hash (#conversation, #questions, #replay/<id>),
  // so tabs are deep-linkable and browser Back/Forward works.
  const { surface, replayId, go, hadInitialHash } = useHashRoute("conversation");
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

  // Pending-question count for the Questions tab badge, so a waiting question is
  // visible from any surface. Lightweight poll, independent of the inbox's own.
  const [pendingCount, setPendingCount] = useState(0);
  useEffect(() => {
    let alive = true;
    const poll = () =>
      listQuestions()
        .then((qs) => alive && setPendingCount(qs.length))
        .catch(() => {});
    poll();
    const id = setInterval(poll, 3000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // The served company's org structure (null when Studio is pointed at a single
  // agency). Fetched once — a served config doesn't change over a session.
  const [company, setCompany] = useState<Company | null>(null);
  useEffect(() => {
    getCompany()
      .then(setCompany)
      .catch(() => setCompany(null));
  }, []);

  // The served roster (null unless Studio runs in `--catalog` mode). When present,
  // the Catalog is the front door and each run targets the picked entry.
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selection, setSelection] = useState<CatalogSelection | null>(null);
  useEffect(() => {
    getCatalog()
      .then((c) => {
        setCatalog(c);
        // Land on the roster in catalog mode, unless the URL already deep-links
        // a specific tab.
        if (c && !hadInitialHash) go("catalog");
      })
      .catch(() => setCatalog(null));
  }, []);

  // Hire a catalog entry: it becomes the run target; start a fresh thread and
  // drop into the conversation to give it a task.
  const hire = (sel: CatalogSelection) => {
    setSelection(sel);
    run.reset();
    go("conversation");
  };
  // A company selection drives the Company org-chart even without a live run yet.
  const activeCompanyOrg = selection?.kind === "company" ? selection.org ?? null : company;

  const openReplay = (sessionId: string, from: Surface) => {
    setReplayFrom(from);
    go("replay", sessionId);
  };

  const empty = surface === "conversation" && !hasThread && run.status === "idle";

  // The app frame is always full-width so switching tabs never resizes it
  // (the old per-surface toggle made the whole frame animate 720<->1440, which
  // read as the conversation "shrinking"). List surfaces fill the frame; the
  // conversation instead centers its own readable column inside the stable
  // frame — see `.thread--narrow` / `.footer--narrow` in styles.css.
  const narrowColumn = surface === "conversation";
  return (
    <div className="app">
      <header className="header">
        <div className="header__brand">
          <span className="header__logo" aria-hidden />
          <span className="header__title">Fabri Studio</span>
        </div>
        <div className="header__right">
          <nav className="tabs" aria-label="views">
            {catalog && (
              <button
                className={"tab" + (surface === "catalog" ? " tab--on" : "")}
                onClick={() => go("catalog")}
              >
                Roster
              </button>
            )}
            <button
              className={"tab" + (surface === "conversation" ? " tab--on" : "")}
              onClick={() => go("conversation")}
            >
              Conversation
            </button>
            <button
              className={"tab" + (surface === "company" ? " tab--on" : "")}
              onClick={() => go("company")}
            >
              Company
            </button>
            <button
              className={"tab" + (surface === "questions" ? " tab--on" : "")}
              onClick={() => go("questions")}
            >
              Questions
              {pendingCount > 0 && (
                <span className="tab__badge" aria-label={`${pendingCount} waiting`}>
                  {pendingCount}
                </span>
              )}
            </button>
            <button
              className={"tab" + (surface === "fleet" ? " tab--on" : "")}
              onClick={() => go("fleet")}
            >
              Fleet
            </button>
            <button
              className={"tab" + (surface === "history" || surface === "replay" ? " tab--on" : "")}
              onClick={() => go("history")}
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

      <main className={"thread" + (narrowColumn ? " thread--narrow" : "")}>
        {surface === "history" && <HistoryList onOpen={(id) => openReplay(id, "history")} />}
        {surface === "questions" && <QuestionsInbox onOpenRun={(id) => openReplay(id, "questions")} />}
        {surface === "fleet" && <FleetView onOpenRun={(id) => openReplay(id, "fleet")} />}
        {surface === "replay" && replayId && (
          <RunReplay key={replayId} sessionId={replayId} onBack={() => go(replayFrom)} />
        )}
        {surface === "catalog" && catalog && <CatalogView catalog={catalog} onRun={hire} />}
        {surface === "company" && (
          activeCompanyOrg ? (
            // A company is served/selected: draw its org chart; overlay live status
            // + cost when a run streams.
            <CompanyOrgChart company={activeCompanyOrg} events={run.turns[activeIdx]?.events} />
          ) : hasThread ? (
            <AgencyGraph events={run.turns[activeIdx]?.events ?? []} />
          ) : (
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
        <footer className="footer footer--narrow">
          <div className="footer__stack">
            {catalog && selection && (
              <div className="running-as">
                <span className="running-as__label">Running</span>
                <span className="running-as__name">{selection.title}</span>
                <span className={"running-as__kind running-as__kind--" + selection.kind}>{selection.kind}</span>
                <button className="running-as__change" onClick={() => go("catalog")}>
                  change
                </button>
              </div>
            )}
            {catalog && !selection && (
              <div className="running-as running-as--empty">
                Pick an agency or company from the <button className="running-as__change" onClick={() => go("catalog")}>Roster</button> to run.
              </div>
            )}
            {hasThread && !busy && (
              <button className="footer__new" onClick={run.reset}>
                New thread
              </button>
            )}
            <Composer
              onSubmit={(task) => run.start(task, selection?.ref)}
              onCancel={run.cancel}
              busy={busy}
              followup={hasThread}
            />
          </div>
        </footer>
      )}
    </div>
  );
}
