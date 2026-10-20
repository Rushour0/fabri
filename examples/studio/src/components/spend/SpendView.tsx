import { useMemo, useState } from "react";
import {
  callWhere,
  dayBars,
  DAYS_SHOWN,
  formatTime,
  formatTokens,
  formatUsd,
  paging,
  percent,
  purposeLabel,
  shares,
  shortId,
  SPEND_PAGE,
  spendApi,
  windowStart,
  type Share,
  type SpendRow,
  type SpendTotal,
} from "../../lib/spend";
import { Empty, ErrorBox, Loading, useLoad } from "../setups/shared";
import "../setups/setups.css";
import "./spend.css";

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="su-card sp-stat">
      <p className="su-card__h">{label}</p>
      <p className="sp-stat__n">{value}</p>
      {sub && <p className="su-dim">{sub}</p>}
    </div>
  );
}

function ShareList({ title, items, onPick, picked }: { title: string; items: Share[]; onPick?: (key: string) => void; picked?: string }) {
  return (
    <div className="su-card">
      <h3 className="su-card__h">{title}</h3>
      {items.length === 0 ? (
        <p className="su-dim">No calls yet.</p>
      ) : (
        <ul className="sp-shares">
          {items.map((s) => {
            const pickable = onPick && s.key !== "__rest";
            const body = (
              <>
                <span className="sp-shares__name">{s.label}</span>
                <span className="sp-shares__bar" aria-hidden>
                  <span style={{ width: `${Math.max(s.share * 100, s.cost_usd > 0 ? 1 : 0)}%` }} />
                </span>
                <span className="su-num sp-shares__usd">{formatUsd(s.cost_usd)}</span>
                <span className="su-num su-dim sp-shares__pct">{percent(s.share)}</span>
              </>
            );
            return (
              <li key={s.key}>
                {pickable ? (
                  <button
                    className={"sp-shares__row" + (picked === s.key ? " sp-shares__row--on" : "")}
                    onClick={() => onPick(picked === s.key ? "" : s.key)}
                    title={`${s.calls} calls. Click to filter.`}
                  >
                    {body}
                  </button>
                ) : (
                  <div className="sp-shares__row" title={`${s.calls} calls`}>{body}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function notes(t: SpendTotal): string[] {
  const out: string[] = [];
  if (t.estimated_usd > 0) out.push(`${formatUsd(t.estimated_usd)} is estimated from tokens`);
  if (t.backfill_usd > 0) out.push(`${formatUsd(t.backfill_usd)} is backfilled history`);
  if (t.failed > 0) out.push(`${t.failed} calls failed`);
  return out;
}

function Flags({ r }: { r: SpendRow }) {
  return (
    <>
      {!r.ok && <span className="su-badge sp-flag--err">failed</span>}
      {r.estimated && <span className="su-badge" title="Priced from tokens; the provider did not report a cost">est</span>}
      {r.backfill && <span className="su-badge su-badge--inherited" title="Imported from the stores that predate the ledger">backfill</span>}
    </>
  );
}

export function SpendView() {
  const [purpose, setPurpose] = useState("");
  const [page, setPage] = useState(0);
  const [tick, setTick] = useState(0);
  const since = useMemo(() => windowStart(new Date()), [tick]);

  const summary = useLoad(() => spendApi.summary(), `s:${tick}`);
  const days = useLoad(() => spendApi.query({ group: "day", since, purpose, limit: 1 }), `d:${since}:${purpose}:${tick}`);
  const models = useLoad(() => spendApi.query({ group: "model", purpose, limit: 1 }), `m:${purpose}:${tick}`);
  const offset = page * SPEND_PAGE;
  const calls = useLoad(() => spendApi.query({ purpose, limit: SPEND_PAGE, offset }), `c:${purpose}:${offset}:${tick}`);

  const pick = (p: string) => {
    setPurpose(p);
    setPage(0);
  };
  const bars = useMemo(() => dayBars(days.data?.groups ?? [], new Date()), [days.data]);
  const pg = paging(calls.data?.rows_total ?? 0, page);
  const purposes = calls.data?.purposes ?? summary.data?.by_purpose.map((p) => p.key) ?? [];
  const peak = Math.max(0, ...bars.map((b) => b.cost_usd));
  const total = models.data?.total;

  return (
    <section className="setups spend" aria-label="Spend">
      <header className="su-head">
        <div>
          <h2 className="catalog__title">Spend</h2>
          <p className="catalog__sub">
            Every model call the local Playbook agents make, from pba's spend ledger. Days are UTC.
          </p>
        </div>
        <button className="btn" onClick={() => setTick((t) => t + 1)}>Refresh</button>
      </header>

      {summary.error ? (
        <ErrorBox message={summary.error} onRetry={summary.reload} />
      ) : summary.loading && !summary.data ? (
        <Loading what="spend" />
      ) : summary.data ? (
        <div className="sp-stats">
          <Stat label="Total" value={formatUsd(summary.data.all.cost_usd)} sub={`${summary.data.all.calls.toLocaleString("en-US")} calls`} />
          <Stat label="Today" value={formatUsd(summary.data.today.cost_usd)} sub={`${summary.data.today.calls.toLocaleString("en-US")} calls`} />
          <Stat label="Last 7 days" value={formatUsd(summary.data["7d"].cost_usd)} sub={`${summary.data["7d"].calls.toLocaleString("en-US")} calls`} />
        </div>
      ) : null}

      {total && notes(total).length > 0 && (
        <p className="su-note">
          {purpose ? `${purposeLabel(purpose)}: ` : ""}
          {notes(total).join(", ")}.
        </p>
      )}

      <div className="su-card">
        <div className="sp-card-head">
          <h3 className="su-card__h">Per day, last {DAYS_SHOWN} days{purpose ? `, ${purposeLabel(purpose)}` : ""}</h3>
          {peak > 0 && <span className="su-dim">peak {formatUsd(peak)}</span>}
        </div>
        {days.error ? (
          <ErrorBox message={days.error} onRetry={days.reload} />
        ) : (
          <div className="sp-bars" role="img" aria-label={`Spend per day for the last ${DAYS_SHOWN} days`}>
            {bars.map((b) => (
              <div key={b.day} className="sp-bars__col" title={`${b.day}: ${formatUsd(b.cost_usd)}, ${b.calls} calls`}>
                <span className={"sp-bars__bar" + (b.cost_usd > 0 ? "" : " sp-bars__bar--zero")} style={{ height: `${Math.max(b.height * 100, b.cost_usd > 0 ? 2 : 0)}%` }} />
              </div>
            ))}
          </div>
        )}
        <div className="sp-bars__axis su-dim">
          <span>{bars[0]?.day.slice(5)}</span>
          <span>{bars[bars.length - 1]?.day.slice(5)}</span>
        </div>
      </div>

      <div className="sp-grid">
        <ShareList title="By purpose, all time" items={shares(summary.data?.by_purpose ?? [], 14, purposeLabel)} onPick={pick} picked={purpose} />
        <ShareList title={`By model${purpose ? `, ${purposeLabel(purpose)}` : ""}`} items={shares(models.data?.groups ?? [])} />
      </div>

      <div className="su-card">
        <div className="sp-card-head">
          <h3 className="su-card__h">Recent calls</h3>
          <div className="su-field su-field--inline">
            <label className="su-field__label" htmlFor="sp-purpose">Purpose</label>
            <select id="sp-purpose" className="su-input su-input--narrow" value={purpose} onChange={(e) => pick(e.target.value)}>
              <option value="">All</option>
              {purposes.map((p) => <option key={p} value={p}>{purposeLabel(p)}</option>)}
            </select>
          </div>
        </div>
        {calls.error ? (
          <ErrorBox message={calls.error} onRetry={calls.reload} />
        ) : calls.loading && !calls.data ? (
          <Loading what="calls" />
        ) : !calls.data || calls.data.rows.length === 0 ? (
          <Empty title="No calls">{purpose ? "Nothing recorded for this purpose yet." : "Nothing recorded yet. Send a chat turn in Playbook."}</Empty>
        ) : (
          <>
            <div className="su-scroll-x">
              <table className="su-table sp-table" aria-label="Recent model calls">
                <thead>
                  <tr>
                    <th scope="col">Time</th>
                    <th scope="col">Purpose</th>
                    <th scope="col">Agent</th>
                    <th scope="col">Model</th>
                    <th scope="col" className="su-num">Tokens in / out</th>
                    <th scope="col" className="su-num">Cost</th>
                    <th scope="col">Thread or task</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.data.rows.map((r) => {
                    const where = callWhere(r);
                    return (
                      <tr key={r.id}>
                        <td className="sp-nowrap">{formatTime(r.ts)}</td>
                        <td className="sp-nowrap">{purposeLabel(r.purpose)}</td>
                        <td>{r.agent_type || <span className="su-dim">-</span>}</td>
                        <td><code>{r.model}</code></td>
                        <td className="su-num sp-nowrap">
                          {r.input_tokens + r.output_tokens === 0 ? <span className="su-dim">-</span> : (
                            <>
                              {formatTokens(r.input_tokens)} / {formatTokens(r.output_tokens)}
                              {r.cached_tokens > 0 && <span className="su-dim"> ({formatTokens(r.cached_tokens)} cached)</span>}
                            </>
                          )}
                        </td>
                        <td className="su-num sp-nowrap">{formatUsd(r.cost_usd)} <Flags r={r} /></td>
                        <td className="sp-nowrap">
                          {where.kind === "none" ? <span className="su-dim">-</span> : (
                            <span title={where.id}><span className="su-dim">{where.kind} </span><code>{shortId(where.id)}</code></span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="su-pager" role="navigation" aria-label="Call pages">
              <span className="su-pager__count" aria-live="polite">{pg.total === 0 ? "0 of 0" : `${pg.from}-${pg.to} of ${pg.total.toLocaleString("en-US")}`}</span>
              <span className="su-actions__spacer" />
              <button className="btn" onClick={() => setPage(0)} disabled={pg.page === 0} aria-label="First page">First</button>
              <button className="btn" onClick={() => setPage(pg.page - 1)} disabled={pg.page === 0} aria-label="Previous page">Prev</button>
              <span className="su-dim">Page {pg.page + 1} of {pg.pages}</span>
              <button className="btn" onClick={() => setPage(pg.page + 1)} disabled={pg.page >= pg.pages - 1} aria-label="Next page">Next</button>
              <button className="btn" onClick={() => setPage(pg.pages - 1)} disabled={pg.page >= pg.pages - 1} aria-label="Last page">Last</button>
            </div>
          </>
        )}
      </div>
    </section>
  );
}
