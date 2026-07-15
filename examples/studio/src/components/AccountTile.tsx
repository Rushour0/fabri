import type { SessionSummary } from "../lib/api";

// One member of a fleet — an account/item's pipeline run. Shows its label,
// status, and its own COGS. Clicking opens the run read-only (the drill-down).

const STATUS_LABEL: Record<string, string> = {
  running: "Running",
  done: "Done",
  error: "Blocked",
  cancelled: "Cancelled",
};

function money(n: number | undefined | null): string | null {
  if (n == null) return null;
  return "$" + (n < 0.01 ? n.toFixed(5) : n.toFixed(4));
}

export function AccountTile({
  session,
  onOpen,
}: {
  session: SessionSummary;
  onOpen: (sessionId: string) => void;
}) {
  const cost = money(session.cost?.total_cost_usd);
  const name = session.label ?? session.task ?? session.session_id.slice(0, 8);
  return (
    <button
      className={`tile tile--${session.status}`}
      onClick={() => onOpen(session.session_id)}
      title={session.task ?? name}
    >
      <div className="tile__top">
        <span className="tile__name">{name}</span>
        <span className={`tile__dot tile__dot--${session.status}`} aria-hidden />
      </div>
      <div className="tile__foot">
        <span className="tile__status">{STATUS_LABEL[session.status] ?? session.status}</span>
        {cost && <span className="tile__cost">{cost}</span>}
      </div>
    </button>
  );
}
