import { useEffect, useMemo, useState } from "react";
import {
  allScopes,
  BASELINE,
  KIND_LABEL,
  KINDS,
  setupsApi,
  type Kind,
  type Scope,
} from "../../lib/setups";
import { AgentTypesView } from "./AgentTypesView";
import { CompaniesView } from "./CompaniesView";
import { ItemsPanel } from "./ItemsPanel";
import { ProjectsView } from "./ProjectsView";
import { ResolvedView } from "./ResolvedView";
import { ScopeTree } from "./ScopeTree";
import { TabBar, useLoad } from "./shared";
import { ToolsPanel } from "./ToolsPanel";
import "./setups.css";

type Mode = "agents" | "companies" | "projects" | "tools" | "merged" | "scopes";

const BASE_TABS: { id: Mode; label: string }[] = [
  { id: "agents", label: "Agent types" },
  { id: "companies", label: "Companies" },
  { id: "projects", label: "Projects" },
  { id: "tools", label: "Tools" },
  { id: "merged", label: "Merged view" },
];

function ScopePicker({ scopes, value, onChange }: { scopes: Scope[]; value: Scope; onChange: (s: Scope) => void }) {
  return (
    <div className="su-field su-field--inline">
      <label className="su-field__label" htmlFor="su-scope-pick">Scope</label>
      <select id="su-scope-pick" className="su-input" value={value.id} onChange={(e) => onChange(scopes.find((s) => s.id === e.target.value) ?? BASELINE)}>
        {scopes.map((s) => (
          <option key={s.id} value={s.id}>{s.id}</option>
        ))}
      </select>
    </div>
  );
}

// Packs and org-level overrides stay supported, behind the Advanced toggle.
function ScopesMode({
  tree,
  scope,
  setScope,
}: {
  tree: ReturnType<typeof useScopeTree>;
  scope: Scope;
  setScope: (s: Scope) => void;
}) {
  const [kind, setKind] = useState<Kind>("agents");
  return (
    <div className="su-layout">
      <aside className="su-side">
        <ScopeTree
          tree={tree.data}
          loading={tree.loading}
          error={tree.error}
          onRetry={tree.reload}
          selected={scope}
          onSelect={setScope}
          onCreated={(s) => {
            tree.reload();
            setScope(s);
          }}
        />
      </aside>
      <div className="su-main">
        <div className="su-main__head">
          <h2 className="su-h2">{scope.id}</h2>
          <TabBar label="Setup kinds" value={kind} onChange={setKind} tabs={KINDS.map((k) => ({ id: k, label: KIND_LABEL[k] }))} />
        </div>
        {kind === "tools" ? <ToolsPanel key={scope.id} scope={scope} /> : <ItemsPanel key={`${scope.id}|${kind}`} scope={scope} kind={kind} />}
      </div>
    </div>
  );
}

function useScopeTree() {
  return useLoad(() => setupsApi.scopes(), "scopes");
}

export function SetupsView() {
  const tree = useScopeTree();
  const [mode, setMode] = useState<Mode>("agents");
  const [advanced, setAdvanced] = useState(false);
  const [project, setProject] = useState<Scope | null>(null);
  const [scope, setScope] = useState<Scope>(BASELINE);
  const [toolScope, setToolScope] = useState<Scope>(BASELINE);
  const scopes = useMemo(() => (tree.data ? allScopes(tree.data) : [BASELINE]), [tree.data]);

  // Leaving Advanced while on its tab would strand the view.
  useEffect(() => {
    if (!advanced && mode === "scopes") setMode("agents");
  }, [advanced, mode]);

  const tabs = advanced ? [...BASE_TABS, { id: "scopes" as Mode, label: "Scopes" }] : BASE_TABS;

  return (
    <section className="setups" aria-label="Setups">
      <header className="su-head">
        <div>
          <h2 className="catalog__title">Setups</h2>
          <p className="catalog__sub">
            Agent types carry the global prompt and skills. Each company picks the types it uses. Projects add local skills,
            context and data.
          </p>
        </div>
        <label className="su-adv">
          <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} />
          <span>Advanced</span>
          <span className="su-dim">packs, org overrides, scopes</span>
        </label>
      </header>

      <TabBar label="Setups sections" value={mode} onChange={setMode} tabs={tabs} />

      <div className="su-body" role="tabpanel" aria-labelledby={`su-tab-${mode}`}>
        {mode === "agents" && <AgentTypesView />}
        {mode === "companies" && (
          <CompaniesView
            tree={tree.data}
            loading={tree.loading}
            error={tree.error}
            onRetry={tree.reload}
            onTreeChanged={tree.reload}
            onOpenProject={(s) => {
              setProject(s);
              setMode("projects");
            }}
          />
        )}
        {mode === "projects" && (
          <ProjectsView
            tree={tree.data}
            loading={tree.loading}
            error={tree.error}
            onRetry={tree.reload}
            onTreeChanged={tree.reload}
            selected={project}
            onSelect={setProject}
          />
        )}
        {mode === "tools" && (
          <div className="su-main su-main--full">
            <ScopePicker scopes={scopes} value={toolScope} onChange={setToolScope} />
            <ToolsPanel key={toolScope.id} scope={toolScope} />
          </div>
        )}
        {mode === "merged" && (
          <div className="su-main su-main--full">
            <ResolvedView tree={tree.data} initial={{ org: project?.org, project: project?.project }} />
          </div>
        )}
        {mode === "scopes" && <ScopesMode tree={tree} scope={scope} setScope={setScope} />}
      </div>
    </section>
  );
}
