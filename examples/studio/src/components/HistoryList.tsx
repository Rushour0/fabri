import { useEffect, useState } from "react";
import { listRuns, type SessionSummary } from "../lib/api";

// Run history — the persisted session index (GET /runs), which survives a
// `fabri serve` restart. Grouped by thread so a multi-turn conversation reads as
// one entry list, newest first. Selecting a run opens it read-only.

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

export function HistoryList({ onOpen }: { onOpen: (sessionId: string) => void }) {
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
  }, []);

  if (error) return <div className="error-banner">Couldn't load history: {error}</div>;
  if (!sessions) return <div className="history__empty">Loading history…</div>;
  if (sessions.length === 0)
    return <div className="history__empty">No runs yet. Start one from the conversation.</div>;

  return (
    <ul className="history">
      {sessions.map((s) => {
        const cost = money(s.cost?.total_cost_usd);
        return (
          <li key={s.session_id}>
            <button className="history__item" onClick={() => onOpen(s.session_id)}>
              <span className={`history__badge history__badge--${s.status}`}>
                {STATUS_LABEL[s.status] ?? s.status}
              </span>
              <span className="history__task">{s.task ?? s.session_id}</span>
              <span className="history__meta">
                {cost && <span className="history__cost">{cost}</span>}
                <span className="history__time">{when(s.started_ts)}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
