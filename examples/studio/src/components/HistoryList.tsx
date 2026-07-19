import { useEffect, useState } from "react";
import { listRuns, type SessionSummary } from "../lib/api";

// Saved conversations from the persisted session index (GET /runs). Multiple
// runs sharing a thread_id collapse into one sidebar row: the original task is
// its title, while selecting it opens the newest run in that conversation.

const STATUS_LABEL: Record<string, string> = {
  running: "Running",
  done: "Done",
  error: "Error",
  cancelled: "Cancelled",
};

function money(n: number | undefined | null): string | null {
  if (n == null) return null;
  return "$" + (n < 0.01 ? n.toFixed(5) : n.toFixed(4));
}

function when(ts: number | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function HistoryList({
  onOpen,
  refreshKey,
}: {
  onOpen: (sessionId: string) => void;
  refreshKey?: string;
}) {
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    listRuns()
      .then((s) => live && setSessions(s))
      .catch((e) => live && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      live = false;
    };
  }, [refreshKey]);

  if (error) return <div className="error-banner">Couldn't load history: {error}</div>;
  if (!sessions) return <div className="history__empty">Loading history…</div>;
  if (sessions.length === 0)
    return <div className="history__empty">Your saved conversations will appear here.</div>;

  const conversations = new Map<
    string,
    { latest: SessionSummary; title: string; turns: number }
  >();
  for (const session of sessions) {
    const key = session.thread_id ?? session.session_id;
    const existing = conversations.get(key);
    if (existing) {
      existing.turns += 1;
      if (session.task) existing.title = session.task;
    } else {
      conversations.set(key, {
        latest: session,
        title: session.task ?? session.session_id,
        turns: 1,
      });
    }
  }

  return (
    <ul className="history history--sidebar">
      {[...conversations.entries()].map(([key, conversation]) => {
        const { latest, title, turns } = conversation;
        const cost = money(latest.cost?.total_cost_usd);
        return (
          <li key={key}>
            <button className="history__item" onClick={() => onOpen(latest.session_id)} title={title}>
              <span className="history__task">{title}</span>
              <span className="history__meta">
                <span className={`history__state history__state--${latest.status}`}>
                  {STATUS_LABEL[latest.status] ?? latest.status}
                </span>
                {turns > 1 && <span>{turns} turns</span>}
                {cost && <span className="history__cost">{cost}</span>}
                <span className="history__time">{when(latest.started_ts)}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
