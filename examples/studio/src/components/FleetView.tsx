import { useCallback, useEffect, useRef, useState } from "react";
import {
  getFleetStatus,
  listFleets,
  submitFleet,
  type FleetStatus,
  type FleetSummary,
} from "../lib/api";
import { AccountTile } from "./AccountTile";

// Fleet mode — Studio as the whole-agency UI. One agency_start fans out to N
// per-item pipelines; this rolls them up (status counts + total COGS + cost by
// model) over a grid of account tiles, and drills into any one run. Launch a new
// fleet by pasting one line per item (optionally "label: task").

function money(n: number | undefined): string {
  if (n == null) return "$0.0000";
  return "$" + (n < 0.01 ? n.toFixed(5) : n.toFixed(4));
}

function parseItems(text: string): { task: string; label?: string }[] {
  return text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean)
    .map((line) => {
      const m = line.match(/^([^:]{1,40}):\s*(.+)$/);
      if (m) return { label: m[1].trim(), task: m[2].trim() };
      return { task: line };
    });
}

function FleetLauncher({ onLaunched }: { onLaunched: (fleetId: string) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const items = parseItems(text);

  const launch = async () => {
    if (!items.length || busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await submitFleet(items);
      setText("");
      onLaunched(res.fleet_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="launcher">
      <div className="launcher__label">New fleet · one item per line (optional "label: task")</div>
      <textarea
        className="launcher__input"
        rows={3}
        value={text}
        placeholder={"acme: onboard the Acme account\nglobex: onboard the Globex account"}
        onChange={(e) => setText(e.target.value)}
        disabled={busy}
      />
      {error && <div className="ask__error">{error}</div>}
      <div className="launcher__actions">
        <span className="launcher__hint">{items.length} item{items.length === 1 ? "" : "s"}</span>
        <button className="btn btn--primary" onClick={launch} disabled={busy || !items.length}>
          {busy ? "Launching…" : "Launch fleet"}
        </button>
      </div>
    </div>
  );
}

function FleetDetail({
  fleetId,
  onBack,
  onOpenRun,
}: {
  fleetId: string;
  onBack: () => void;
  onOpenRun: (sessionId: string) => void;
}) {
  const [status, setStatus] = useState<FleetStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const s = await getFleetStatus(fleetId);
      setStatus(s);
      // Keep polling while any member is still running.
      if ((s.counts.running ?? 0) > 0 && timer.current == null) {
        timer.current = window.setInterval(refresh, 2000);
      } else if ((s.counts.running ?? 0) === 0 && timer.current != null) {
        window.clearInterval(timer.current);
        timer.current = null;
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [fleetId]);

  useEffect(() => {
    refresh();
    return () => {
      if (timer.current != null) window.clearInterval(timer.current);
      timer.current = null;
    };
  }, [refresh]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!status) return <div className="history__empty">Loading fleet…</div>;

  const c = status.counts;
  const byModel = Object.entries(status.totals.cost_by_model ?? {});

  return (
    <div className="fleet">
      <div className="replay__bar">
        <button className="btn replay__back" onClick={onBack}>
          ← Fleets
        </button>
        <span className="replay__label">Fleet roll-up</span>
      </div>

      <section className="fleet__rollup">
        <div className="fleet__counts">
          <Count label="done" n={c.done} kind="done" />
          <Count label="running" n={c.running} kind="running" />
          <Count label="blocked" n={c.error} kind="error" />
          {c.cancelled ? <Count label="cancelled" n={c.cancelled} kind="cancelled" /> : null}
        </div>
        <div className="fleet__cogs">
          <span className="cogs__label">Fleet COGS</span>
          <span className="cogs__total">{money(status.totals.total_cost_usd)}</span>
        </div>
        {byModel.length > 0 && (
          <ul className="cogs__models fleet__models">
            {byModel.map(([m, cost]) => (
              <li key={m} className="cogs__model">
                <span className="cogs__model-name">{m}</span>
                <span className="cogs__model-cost">{money(cost)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <div className="tiles">
        {status.sessions.map((s) => (
          <AccountTile key={s.session_id} session={s} onOpen={onOpenRun} />
        ))}
      </div>
    </div>
  );
}

function Count({ label, n, kind }: { label: string; n?: number; kind: string }) {
  return (
    <div className={`fleet__count fleet__count--${kind}`}>
      <span className="fleet__count-n">{n ?? 0}</span>
      <span className="fleet__count-l">{label}</span>
    </div>
  );
}

export function FleetView({ onOpenRun }: { onOpenRun: (sessionId: string) => void }) {
  const [fleets, setFleets] = useState<FleetSummary[] | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    listFleets()
      .then(setFleets)
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (openId) {
    return (
      <FleetDetail
        fleetId={openId}
        onBack={() => {
          setOpenId(null);
          load();
        }}
        onOpenRun={onOpenRun}
      />
    );
  }

  return (
    <div className="fleet">
      <FleetLauncher
        onLaunched={(id) => {
          setOpenId(id);
        }}
      />
      {error && <div className="error-banner">{error}</div>}
      {fleets && fleets.length === 0 && (
        <div className="history__empty">No fleets yet. Launch one above.</div>
      )}
      {fleets && fleets.length > 0 && (
        <ul className="history">
          {fleets.map((f) => (
            <li key={f.fleet_id}>
              <button className="history__item" onClick={() => setOpenId(f.fleet_id)}>
                <span className="history__badge">{f.size} runs</span>
                <span className="history__task">
                  {(f.counts.done ?? 0)} done · {(f.counts.running ?? 0)} running
                  {f.counts.error ? ` · ${f.counts.error} blocked` : ""}
                </span>
                <span className="history__meta">
                  <span className="history__cost">{money(f.totals.total_cost_usd)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
