import { useState } from "react";
import { enrichTools, setupsApi, type Resolved, type ResolvedItem, type ScopeTree } from "../../lib/setups";
import { ApprovalBadge, Empty, ErrorBox, LayerBadge, Loading, useLoad } from "./shared";

const GROUPS: { key: keyof Resolved["groups"]; label: string }[] = [
  { key: "agents", label: "Agent types" },
  { key: "skills", label: "Skills" },
  { key: "context", label: "Context" },
  { key: "data", label: "Data" },
  { key: "plays", label: "Plays" },
];

function Group({ label, items }: { label: string; items: ResolvedItem[] }) {
  return (
    <section className="su-card" aria-label={label}>
      <h4 className="su-card__h">{label} <span className="su-dim">{items.length}</span></h4>
      {items.length === 0 ? (
        <p className="su-dim">None in the merged setup.</p>
      ) : (
        <ul className="su-reslist">
          {items.map((it) => (
            <li key={it.name} className="su-reslist__item">
              <code>{it.name}</code>
              <LayerBadge source={it.source} />
              {!it.enabled && <span className="su-badge su-badge--off">disabled</span>}
              {it.detail && it.detail !== it.name && <span className="su-reslist__detail">{it.detail}</span>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** Debug view: the merged setup for one company, project and agent type. */
export function ResolvedView({ tree, initial }: { tree: ScopeTree | null; initial?: { org?: string; project?: string } }) {
  const [org, setOrg] = useState(initial?.org ?? "");
  const [project, setProject] = useState(initial?.project ?? "");
  const [type, setType] = useState("");
  const projects = tree?.orgs.find((o) => o.name === org)?.projects ?? [];
  const key = `${org}|${project}|${type}`;

  const res = useLoad(() => setupsApi.resolved(org || undefined, project || undefined, type || undefined), key);
  // Agent type options come from the merged setup without a type picked.
  const types = useLoad(() => setupsApi.resolved(org || undefined, project || undefined), `types|${org}|${project}`);
  const cat = useLoad(() => setupsApi.catalog(org || undefined, project || undefined, type || undefined), `cat|${key}`);

  const typeNames = types.data?.groups.agents.map((a) => a.name) ?? [];
  const tools = res.data ? enrichTools(res.data.tools, cat.data ?? []) : [];

  return (
    <div className="su-panel">
      <div className="su-panel__bar">
        <p className="su-panel__sub">
          What an agent actually sees: baseline, packs, company and project layers merged. Every item carries the layer it came from.
        </p>
      </div>
      <div className="su-pickers">
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-r-org">Company</label>
          <select id="su-r-org" className="su-input" value={org} onChange={(e) => { setOrg(e.target.value); setProject(""); setType(""); }}>
            <option value="">(none, baseline only)</option>
            {tree?.orgs.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
          </select>
        </div>
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-r-proj">Project</label>
          <select id="su-r-proj" className="su-input" value={project} onChange={(e) => setProject(e.target.value)} disabled={!org}>
            <option value="">(none)</option>
            {projects.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-r-type">Agent type</label>
          <select id="su-r-type" className="su-input" value={type} onChange={(e) => setType(e.target.value)}>
            <option value="">(all types)</option>
            {typeNames.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
      </div>

      {res.loading && !res.data && <Loading what="merged setup" />}
      {res.error && <ErrorBox message={res.error} onRetry={res.reload} />}
      {res.data && (
        <>
          {res.data.layers.length > 0 && (
            <p className="su-chain" aria-label="Layer order">
              {res.data.layers.map((l, i) => (
                <span key={l}>{i > 0 && <span className="su-chain__arrow" aria-hidden> then </span>}<LayerBadge source={l} /></span>
              ))}
            </p>
          )}
          {res.data.warnings.length > 0 && (
            <ul className="su-lint__list" aria-label="Warnings">
              {res.data.warnings.map((w, i) => (
                <li key={i} className="su-lint__item su-lint__item--warning"><span className="su-lint__sev">warning</span><span className="su-lint__msg">{w}</span></li>
              ))}
            </ul>
          )}
          <div className="su-resgrid">
            {GROUPS.map((g) => <Group key={g.key} label={g.label} items={res.data!.groups[g.key]} />)}
          </div>

          <section aria-label="Tool set">
            <h3 className="su-h3">Tool set {type && <span className="su-dim">for {type}</span>}</h3>
            {tools.length === 0 ? (
              <Empty title="No tools resolved">{type ? "This type has no tools in the merged setup." : "No tools in the merged setup."}</Empty>
            ) : (
              <div className="su-scroll-x">
                <table className="su-table" aria-label="Resolved tools">
                  <thead><tr><th scope="col">Title</th><th scope="col">Name</th><th scope="col">Approval</th><th scope="col">Source</th></tr></thead>
                  <tbody>
                    {tools.map((t) => (
                      <tr key={t.name}>
                        <td>{t.title || <span className="su-dim">-</span>}</td>
                        <th scope="row"><code>{t.name}</code></th>
                        <td><ApprovalBadge approval={t.approval} /></td>
                        <td>{t.source ? <LayerBadge source={t.source} /> : <span className="su-dim">-</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section aria-label="Route options">
            <h3 className="su-h3">Route options</h3>
            {res.data.routes.length === 0 ? (
              <Empty title="No route options">An agent type shows up here when its agent.yaml has a route with description and not_for.</Empty>
            ) : (
              <div className="su-scroll-x">
                <table className="su-table" aria-label="Route options">
                  <thead><tr><th scope="col">Type</th><th scope="col">Use for</th><th scope="col">Not for</th><th scope="col">Source</th></tr></thead>
                  <tbody>
                    {res.data.routes.map((r) => (
                      <tr key={r.type}>
                        <th scope="row"><code>{r.type}</code></th>
                        <td>{r.description}</td>
                        <td>{r.not_for}</td>
                        <td>{r.source ? <LayerBadge source={r.source} /> : null}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {res.data.systemPrompt && (
            <details className="su-details">
              <summary>System prompt for {type}</summary>
              <pre className="su-pre">{res.data.systemPrompt}</pre>
            </details>
          )}
        </>
      )}
    </div>
  );
}
