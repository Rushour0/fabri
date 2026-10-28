import { useEffect, useMemo, useState } from "react";
import {
  allScopes,
  BASELINE,
  DEFAULT_UI,
  KIND_LABEL,
  KINDS,
  setupsApi,
  type Kind,
  type Scope,
  type UiConfig,
  type UiTab,
} from "../../lib/setups";
import { CompaniesView } from "./CompaniesView";
import { GlobalSkillsView } from "./GlobalSkillsView";
import { OrgTypesView } from "./OrgTypesView";
import { PreviewView } from "./PreviewView";
import { ItemsPanel } from "./ItemsPanel";
import { ProjectsView } from "./ProjectsView";
import { ResolvedView } from "./ResolvedView";
import { ScopeTree } from "./ScopeTree";
import { TabBar, useLoad } from "./shared";
import { ToolsPanel } from "./ToolsPanel";
import { UiConfigContext } from "./uiConfig";
import "./setups.css";

type Mode = UiTab | "scopes";

const TAB_LABEL_KEY = {
  agents: "agent_types",
  skills: "global_skills",
  companies: "companies",
  projects: "projects",
  tools: "tools",
  preview: "preview",
  merged: "merged",
} as const;

function baseTabs(ui: UiConfig): { id: Mode; label: string }[] {
  return ui.tabs.map((id) => ({ id, label: ui.labels[TAB_LABEL_KEY[id]] }));
}

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
  const uiLoad = useLoad(() => setupsApi.ui(), "ui");
  const ui = uiLoad.data ?? DEFAULT_UI;
  const [picked, setMode] = useState<Mode | null>(null);
  const mode: Mode = picked ?? ui.defaultTab;
  const [advanced, setAdvanced] = useState(false);
  const [project, setProject] = useState<Scope | null>(null);
  const [scope, setScope] = useState<Scope>(BASELINE);
  const [toolScope, setToolScope] = useState<Scope>(BASELINE);
  const scopes = useMemo(() => (tree.data ? allScopes(tree.data) : [BASELINE]), [tree.data]);

  // Leaving Advanced while on its tab would strand the view.
  useEffect(() => {
    if (!advanced && mode === "scopes") setMode(null);
  }, [advanced, mode]);

  const base = baseTabs(ui);
  const tabs = advanced ? [...base, { id: "scopes" as Mode, label: "Scopes" }] : base;

  return (
    <UiConfigContext.Provider value={ui}>
    <section className="setups" aria-label={ui.title}>
      <header className="su-head">
        <div>
          <h2 className="catalog__title">{ui.title}</h2>
          <p className="catalog__sub">{ui.intro}</p>
        </div>
        <label className="su-adv">
          <input type="checkbox" checked={advanced} onChange={(e) => setAdvanced(e.target.checked)} />
          <span>Advanced</span>
          <span className="su-dim">packs, org overrides, scopes</span>
        </label>
      </header>

      <TabBar label="Setups sections" value={mode} onChange={setMode} tabs={tabs} />

      <div className="su-body" role="tabpanel" aria-labelledby={`su-tab-${mode}`}>
        {mode === "agents" && <OrgTypesView tree={tree.data} loading={tree.loading} error={tree.error} onRetry={tree.reload} />}
        {mode === "skills" && <GlobalSkillsView />}
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
        {mode === "preview" && (
          <div className="su-main su-main--full">
            <PreviewView tree={tree.data} initial={{ org: project?.org, project: project?.project }} />
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
    </UiConfigContext.Provider>
  );
}
