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
import { SetupsView } from "./components/setups/SetupsView";
import { SpendView } from "./components/spend/SpendView";
import { CatalogView, type CatalogSelection } from "./components/CatalogView";
import ConnectSlack from "./components/ConnectSlack";
import ConnectGitHub from "./components/ConnectGitHub";
import ConnectLinear from "./components/ConnectLinear";
import { useHashRoute, type Surface } from "./hooks/useHashRoute";
import { listQuestions, getCompany, getCatalog, onUnauthorized, type Company, type Catalog } from "./lib/api";
import { getAuthConfig, getMe, LEGACY_AUTH_CONFIG, logout, mustSignIn, type AuthConfig, type AuthState } from "./lib/auth";
import { LoginScreen, type AuthReason } from "./components/LoginScreen";
import { hasSection, landingSurface, STUDIO_SECTIONS, surfaceAllowed, type Section } from "./lib/sections";

// VITE_STUDIO_SECTIONS picks the top-level sections; unset shows all of them.
const show = (s: Section) => hasSection(STUDIO_SECTIONS, s);
const HOME = landingSurface(STUDIO_SECTIONS);

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
  const [authState, setAuthState] = useState<AuthState>("loading");
  const [authConfig, setAuthConfig] = useState<AuthConfig>(LEGACY_AUTH_CONFIG);
  const [showAuth, setShowAuth] = useState(false);
  // Which prompt sent the visitor to the login screen, so its copy matches.
  const [authReason, setAuthReason] = useState<AuthReason>("history");
  const requireAuthForIntegrations = () => {
    setAuthReason("integrations");
    setShowAuth(true);
  };
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Tab state lives in the URL hash (#conversation, #questions, #replay/<id>),
  // so tabs are deep-linkable and browser Back/Forward works.
  const { surface, replayId, go, hadInitialHash } = useHashRoute(HOME);
  // Where the replay was opened from, so its back button returns there.
  const [replayFrom, setReplayFrom] = useState<Surface>("history");
  const busy = run.status === "submitting" || run.status === "running";
  const hasThread = run.turns.length > 0;
  const activeIdx = run.turns.length - 1;

  const endRef = useRef<HTMLDivElement>(null);
  const appVisible = authState !== "loading";
  const authEmail = typeof authState === "object" ? authState.email : null;
  const hasAccountAccess = authState === "disabled" || authEmail !== null;

  useEffect(() => {
    let live = true;
    Promise.all([getMe(), getAuthConfig()]).then(([state, cfg]) => {
      if (!live) return;
      setAuthConfig(cfg);
      setAuthState(state);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => onUnauthorized(() => setAuthState("anon")), []);

  // A deep link or a stale hash can point at a hidden section.
  useEffect(() => {
    if (!surfaceAllowed(STUDIO_SECTIONS, surface)) go(HOME);
  }, [go, surface]);

  useEffect(() => {
    if (!appVisible) return;
    if (surface !== "conversation") return;
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [appVisible, run.turns, run.status, surface]);

  useEffect(() => {
    if (
      authState === "anon" &&
      (surface === "history" || surface === "replay" || surface === "questions" || surface === "fleet" || surface === "setups" || surface === "spend")
    ) {
      setShowAuth(true);
    }
  }, [authState, surface]);

  useEffect(() => {
    if (hasAccountAccess && surface === "history") go(HOME);
  }, [go, hasAccountAccess, surface]);

  // Pending-question count for the Questions tab badge, so a waiting question is
  // visible from any surface. Lightweight poll, independent of the inbox's own.
  const [pendingCount, setPendingCount] = useState(0);
  useEffect(() => {
    if (!appVisible || !hasAccountAccess || !show("questions")) {
      setPendingCount(0);
      return;
    }
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
  }, [appVisible, hasAccountAccess]);

  // The served company's org structure (null when Studio is pointed at a single
  // agency). Fetched once — a served config doesn't change over a session.
  const [company, setCompany] = useState<Company | null>(null);
  useEffect(() => {
    if (!appVisible || !show("company")) return;
    getCompany()
      .then(setCompany)
      .catch(() => setCompany(null));
  }, [appVisible]);

  // The served roster (null unless Studio runs in `--catalog` mode). When present,
  // the Catalog is the front door and each run targets the picked entry.
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [selection, setSelection] = useState<CatalogSelection | null>(null);
  useEffect(() => {
    if (!appVisible || !show("roster")) return;
    getCatalog()
      .then((c) => {
        setCatalog(c);
        // Land on the roster in catalog mode, unless the URL already deep-links
        // a specific tab.
        if (c && !hadInitialHash) go("catalog");
      })
      .catch(() => setCatalog(null));
  }, [appVisible, go, hadInitialHash]);

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

  const openSavedConversation = (sessionId: string) => {
    setReplayFrom(surface === "replay" || surface === "history" ? "conversation" : surface);
    setSidebarOpen(false);
    go("replay", sessionId);
  };

  const newConversation = () => {
    run.reset();
    setSidebarOpen(false);
    go("conversation");
  };

  const empty = surface === "conversation" && !hasThread && run.status === "idle";

  // The app frame is always full-width so switching tabs never resizes it
  // (the old per-surface toggle made the whole frame animate 720<->1440, which
  // read as the conversation "shrinking"). List surfaces fill the frame; the
  // conversation instead centers its own readable column inside the stable
  // frame — see `.thread--narrow` / `.footer--narrow` in styles.css.
  const narrowColumn = surface === "conversation";
  const handleLogout = async () => {
    try {
      await logout();
    } finally {
      setAuthState("anon");
      go(HOME);
    }
  };

  if (authState === "loading") {
    return <main style={{ minHeight: "100%", display: "grid", placeItems: "center" }}>Loading…</main>;
  }
  if (mustSignIn(authState, authConfig)) {
    return (
      <LoginScreen
        onAuthed={(email) => {
          setAuthState({ email });
          setShowAuth(false);
        }}
        reason="required"
        allowSignup={authConfig.signup}
      />
    );
  }
  if (showAuth) {
    return (
      <LoginScreen
        onAuthed={(email) => {
          setAuthState({ email });
          setShowAuth(false);
        }}
        onContinueAsGuest={() => {
          setAuthState("anon");
          setShowAuth(false);
          // From the integrations prompt, "not now" should leave the visitor
          // where they were rather than yanking them into the thread.
          if (authReason !== "integrations") go(HOME);
        }}
        reason={authReason}
        allowSignup={authConfig.signup}
      />
    );
  }

  return (
    <div className="app">
      <header className="header">
        <div className="header__brand">
          {show("conversation") && (
            <button className="sidebar-toggle" onClick={() => setSidebarOpen(true)} aria-label="Open conversations">
              Conversations
            </button>
          )}
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
            {show("conversation") && (
              <button
                className={"tab" + (surface === "conversation" ? " tab--on" : "")}
                onClick={() => go("conversation")}
              >
                Conversation
              </button>
            )}
            {show("company") && (
              <button
                className={"tab" + (surface === "company" ? " tab--on" : "")}
                onClick={() => go("company")}
              >
                Company
              </button>
            )}
            {hasAccountAccess && show("questions") && (
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
            )}
            {hasAccountAccess && show("fleet") && (
              <button
                className={"tab" + (surface === "fleet" ? " tab--on" : "")}
                onClick={() => go("fleet")}
              >
                Fleet
              </button>
            )}
            {hasAccountAccess && show("setups") && (
              <button
                className={"tab" + (surface === "setups" ? " tab--on" : "")}
                onClick={() => go("setups")}
              >
                Setups
              </button>
            )}
            {hasAccountAccess && show("spend") && (
              <button
                className={"tab" + (surface === "spend" ? " tab--on" : "")}
                onClick={() => go("spend")}
              >
                Spend
              </button>
            )}
            {/* Settings stays visible signed-out: the integrations are part of
                the pitch, and each one gates its own connect action. */}
            {show("settings") && (
              <button
                className={"tab" + (surface === "settings" ? " tab--on" : "")}
                onClick={() => go("settings")}
              >
                Settings
              </button>
            )}
            </nav>
            {authEmail && (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ maxWidth: 180, overflow: "hidden", color: "var(--text-dim)", fontSize: 12, textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{authEmail}</span>
                <button className="btn" style={{ padding: "5px 9px", fontSize: 12 }} onClick={handleLogout}>
                  Log out
                </button>
              </div>
            )}
            {show("conversation") && (
              <div className={`status status--${run.status}`}>
                <span className="status__dot" aria-hidden />
                {STATUS_LABEL[run.status] ?? run.status}
              </div>
            )}
        </div>
      </header>

      <div className="workspace">
        {show("conversation") && (
        <aside className={"conversation-sidebar" + (sidebarOpen ? " conversation-sidebar--open" : "")} aria-label="Saved conversations">
          <div className="conversation-sidebar__header">
            <span>Conversations</span>
            <button className="conversation-sidebar__new" onClick={newConversation} aria-label="New conversation" title="New conversation">+</button>
            <button className="conversation-sidebar__close" onClick={() => setSidebarOpen(false)} aria-label="Close conversations">×</button>
          </div>
          {hasAccountAccess ? (
            <HistoryList
              onOpen={openSavedConversation}
              refreshKey={`${run.turns.length}:${run.status}`}
            />
          ) : (
            <div className="conversation-sidebar__guest">
              <p>Sign in to save conversations and reopen them later.</p>
              <button className="btn" onClick={() => setShowAuth(true)}>Save conversation history</button>
            </div>
          )}
        </aside>
        )}

        <div className="main-pane">
          <main className={"thread" + (narrowColumn ? " thread--narrow" : "")}>
            {hasAccountAccess && surface === "questions" && <QuestionsInbox onOpenRun={(id) => openReplay(id, "questions")} />}
            {hasAccountAccess && surface === "setups" && <SetupsView />}
            {hasAccountAccess && surface === "spend" && <SpendView />}
            {hasAccountAccess && surface === "fleet" && <FleetView onOpenRun={(id) => openReplay(id, "fleet")} />}
            {surface === "settings" && (
              <div className="settings">
                <div className="settings__head">
                  <p className="settings__eyebrow">Integrations</p>
                  <h2 className="settings__title">Put an agency where the work already is</h2>
                  <p className="settings__sub">
                    The agents you run here can also work in the tools your team already uses. Each
                    one keeps the same behaviour: it shows its plan, tracks its cost, and asks you
                    when it needs a decision.
                  </p>
                  {!hasAccountAccess && (
                    <p className="settings__note">
                      Connecting a workspace needs an account. Everything below is what you get.
                    </p>
                  )}
                </div>
                <div className="settings__grid">
                  <ConnectSlack locked={!hasAccountAccess} onRequireAuth={requireAuthForIntegrations} />
                  <ConnectGitHub locked={!hasAccountAccess} onRequireAuth={requireAuthForIntegrations} />
                  <ConnectLinear locked={!hasAccountAccess} onRequireAuth={requireAuthForIntegrations} />
                </div>
              </div>
            )}
            {hasAccountAccess && surface === "replay" && replayId && (
              <RunReplay key={replayId} sessionId={replayId} onBack={() => go(replayFrom)} />
            )}
            {surface === "catalog" && catalog && <CatalogView catalog={catalog} onRun={hire} />}
            {surface === "company" && (
              activeCompanyOrg ? (
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
                  <button className="footer__new" onClick={newConversation}>
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
      </div>
    </div>
  );
}
