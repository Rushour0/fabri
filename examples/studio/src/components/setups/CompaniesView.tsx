import { useEffect, useState } from "react";
import {
  BASELINE,
  errorText,
  orgScope,
  parseAgentsList,
  projectScope,
  setAgentsList,
  setupsApi,
  type Scope,
  type ScopeTree,
} from "../../lib/setups";
import { CreateForm } from "./ScopeTree";
import { Empty, ErrorBox, Loading, useLoad } from "./shared";

function AgentSelection({ org }: { org: string }) {
  const scope = orgScope(org);
  const types = useLoad(() => setupsApi.items(BASELINE.id, "agents"), "agent-types");
  const setup = useLoad(() => setupsApi.orgSetup(scope.id), scope.id);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [baseline, setBaseline] = useState<string>("");
  const [implicit, setImplicit] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const names = types.data?.map((t) => t.name) ?? [];

  useEffect(() => {
    if (setup.data === null || !types.data) return;
    const list = parseAgentsList(setup.data);
    setImplicit(list === null);
    // No `agents` key yet means no restriction, so every type is on.
    const on = new Set(list ?? names);
    setChecked(on);
    setBaseline([...on].sort().join(","));
    setNote(null);
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [setup.data, types.data]);

  const stale = [...checked].filter((n) => !names.includes(n));
  const all = [...names, ...stale];
  const dirty = [...checked].sort().join(",") !== baseline;

  const toggle = (n: string) =>
    setChecked((c) => {
      const next = new Set(c);
      if (next.has(n)) next.delete(n);
      else next.add(n);
      return next;
    });

  const save = async () => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      const ordered = all.filter((n) => checked.has(n));
      await setupsApi.putOrgSetup(scope.id, setAgentsList(setup.data ?? "", ordered));
      setNote("Saved. Unchecked types are off for this company.");
      setup.reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="su-card" aria-label="Agent types for this company">
      <h3 className="su-h3">Agent types</h3>
      <p className="su-panel__sub">Stored in <code>companies/{org}/setup.yaml</code> under <code>agents</code>. Types left unchecked are off for {org}.</p>
      {(types.loading || setup.loading) && !setup.data && !types.data && <Loading what="agent types" />}
      {types.error && <ErrorBox message={types.error} onRetry={types.reload} />}
      {setup.error && <ErrorBox message={setup.error} onRetry={setup.reload} />}
      {types.data && names.length === 0 && <Empty title="No agent types defined">Create them under Agent types first.</Empty>}
      {types.data && setup.data !== null && (
        <>
          {implicit && <p className="su-note">No list saved yet, so every type is on. Save to make this selection explicit.</p>}
          <fieldset className="su-checks">
            <legend className="su-sr">Agent types used by {org}</legend>
            {all.map((n) => (
              <label key={n} className="su-check">
                <input type="checkbox" checked={checked.has(n)} onChange={() => toggle(n)} />
                <span>{n}</span>
                {!names.includes(n) && <span className="su-badge su-badge--off">not defined</span>}
              </label>
            ))}
          </fieldset>
          {error && <ErrorBox message={error} />}
          {note && !error && <p className="su-note su-note--ok" role="status">{note}</p>}
          <div className="su-actions">
            <button className="btn btn--primary" onClick={save} disabled={busy || (!dirty && !implicit)}>
              {busy ? "Saving…" : "Save selection"}
            </button>
            {dirty && <span className="su-badge su-badge--dirty">unsaved</span>}
          </div>
        </>
      )}
    </section>
  );
}

export function CompaniesView({
  tree,
  loading,
  error,
  onRetry,
  onTreeChanged,
  onOpenProject,
  initialOrg,
}: {
  tree: ScopeTree | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onTreeChanged: () => void;
  onOpenProject: (s: Scope) => void;
  initialOrg?: string;
}) {
  const [sel, setSel] = useState<string | null>(initialOrg ?? null);
  const [creating, setCreating] = useState<null | "org" | "project">(null);
  const org = tree?.orgs.find((o) => o.name === sel);

  return (
    <div className="su-layout">
      <aside className="su-side" aria-label="Companies">
        <div className="su-tree__head">
          <h3 className="su-side-title">Companies</h3>
          <button className="btn" onClick={() => setCreating(creating === "org" ? null : "org")} aria-expanded={creating === "org"}>+ Company</button>
        </div>
        {creating === "org" && (
          <CreateForm kind="org" org={null} onCancel={() => setCreating(null)} onDone={(s) => { setCreating(null); onTreeChanged(); setSel(s.org ?? null); }} />
        )}
        {loading && !tree && <Loading what="companies" />}
        {error && <ErrorBox message={error} onRetry={onRetry} />}
        {tree && tree.orgs.length === 0 && <Empty title="No companies yet">Create the first one.</Empty>}
        <ul className="su-cards">
          {tree?.orgs.map((o) => (
            <li key={o.name}>
              <button className={"su-typecard" + (o.name === sel ? " su-typecard--on" : "")} onClick={() => { setSel(o.name); setCreating(null); }} aria-pressed={o.name === sel}>
                <span className="su-typecard__name">{o.name}</span>
                <span className="su-dim">{o.projects.length} project{o.projects.length === 1 ? "" : "s"}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="su-main">
        {!org && <Empty title="Pick a company">Choose which agent types it uses and see its projects.</Empty>}
        {org && (
          <>
            <div className="su-main__head"><h2 className="su-h2">{org.name}</h2></div>
            <AgentSelection key={org.name} org={org.name} />
            <section className="su-card" aria-label="Projects">
              <div className="su-tree__head">
                <h3 className="su-h3">Projects</h3>
                <button className="btn" onClick={() => setCreating(creating === "project" ? null : "project")} aria-expanded={creating === "project"}>+ Project</button>
              </div>
              {creating === "project" && (
                <CreateForm kind="project" org={org.name} onCancel={() => setCreating(null)} onDone={(s) => { setCreating(null); onTreeChanged(); onOpenProject(s); }} />
              )}
              {org.projects.length === 0 ? (
                <Empty title="No projects yet">A project holds local skills, context and data for one piece of work.</Empty>
              ) : (
                <ul className="su-reslist">
                  {org.projects.map((p) => (
                    <li key={p} className="su-reslist__item">
                      <code>{p}</code>
                      <button className="btn" onClick={() => onOpenProject(projectScope(org.name, p))}>Open project</button>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}
