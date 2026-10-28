import { useState } from "react";
import { projectScope, type Scope, type ScopeTree } from "../../lib/setups";
import { CreateForm } from "./ScopeTree";
import { ItemsPanel } from "./ItemsPanel";
import { Empty, ErrorBox, Loading, TabBar } from "./shared";
import { useLabel } from "./uiConfig";

type PTab = "skills" | "context" | "data";

export function ProjectsView({
  tree,
  loading,
  error,
  onRetry,
  onTreeChanged,
  selected,
  onSelect,
}: {
  tree: ScopeTree | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onTreeChanged: () => void;
  selected: Scope | null;
  onSelect: (s: Scope) => void;
}) {
  const [tab, setTab] = useState<PTab>("skills");
  const label = useLabel();
  const [creatingOrg, setCreatingOrg] = useState<string | null>(null);

  return (
    <div className="su-layout">
      <aside className="su-side" aria-label={label("projects")}>
        <h3 className="su-side-title">{label("projects")}</h3>
        {loading && !tree && <Loading what={label("projects").toLowerCase()} />}
        {error && <ErrorBox message={error} onRetry={onRetry} />}
        {tree && tree.orgs.length === 0 && <Empty title={`No ${label("companies").toLowerCase()} yet`}>Create a {label("company").toLowerCase()} first, then add {label("projects").toLowerCase()} to it.</Empty>}
        {tree?.orgs.map((o) => (
          <div key={o.name} className="su-group">
            <div className="su-tree__head">
              <h4 className="su-group__name">{o.name}</h4>
              <button className="btn" onClick={() => setCreatingOrg(creatingOrg === o.name ? null : o.name)} aria-expanded={creatingOrg === o.name} aria-label={`New project in ${o.name}`}>+</button>
            </div>
            {creatingOrg === o.name && (
              <CreateForm kind="project" org={o.name} onCancel={() => setCreatingOrg(null)} onDone={(s) => { setCreatingOrg(null); onTreeChanged(); onSelect(s); }} />
            )}
            {o.projects.length === 0 && <p className="su-dim">No {label("projects").toLowerCase()}</p>}
            <ul className="su-cards">
              {o.projects.map((p) => {
                const s = projectScope(o.name, p);
                return (
                  <li key={p}>
                    <button className={"su-typecard" + (selected?.id === s.id ? " su-typecard--on" : "")} onClick={() => onSelect(s)} aria-pressed={selected?.id === s.id}>
                      <span className="su-typecard__name">{p}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </aside>
      <div className="su-main">
        {!selected && <Empty title={`Pick a ${label("project").toLowerCase()}`}>Local {label("skills").toLowerCase()}, context and data live here. {label("global_skills")} come from the {label("agent_types").toLowerCase()}, and {label("company").toLowerCase()} context applies to every {label("project").toLowerCase()}.</Empty>}
        {selected && (
          <>
            <div className="su-main__head">
              <h2 className="su-h2"><span className="su-dim">{selected.org} /</span> {selected.project}</h2>
              <TabBar
                label="Project sections"
                value={tab}
                onChange={setTab}
                tabs={[{ id: "skills", label: `Local ${label("skills").toLowerCase()}` }, { id: "context", label: label("context") }, { id: "data", label: label("data") }]}
              />
            </div>
            <ItemsPanel localFirst key={`${selected.id}|${tab}`} scope={selected} kind={tab} />
          </>
        )}
      </div>
    </div>
  );
}
