import { useState, type FormEvent } from "react";
import {
  BASELINE,
  errorText,
  orgScope,
  packScope,
  projectScope,
  setupsApi,
  validName,
  type Scope,
  type ScopeTree as Tree,
} from "../../lib/setups";
import { Empty, ErrorBox, Loading } from "./shared";

export type CreateKind = "pack" | "org" | "project";

function Row({
  scope,
  selected,
  onSelect,
  depth,
  hint,
}: {
  scope: Scope;
  selected: boolean;
  onSelect: (s: Scope) => void;
  depth: 0 | 1 | 2;
  hint?: string;
}) {
  return (
    <li role="none">
      <button
        role="treeitem"
        aria-selected={selected}
        aria-level={depth + 1}
        className={"su-tree__row" + (selected ? " su-tree__row--on" : "")}
        style={{ paddingLeft: 10 + depth * 16 }}
        onClick={() => onSelect(scope)}
      >
        <span className="su-tree__label">{scope.label}</span>
        {hint && <span className="su-tree__hint">{hint}</span>}
      </button>
    </li>
  );
}

export function CreateForm({
  kind,
  org,
  onDone,
  onCancel,
}: {
  kind: CreateKind;
  org: string | null;
  onDone: (s: Scope) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bad = name !== "" && !validName(name);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!validName(name) || busy) return;
    setBusy(true);
    setError(null);
    try {
      await setupsApi.createScope({ kind, name, ...(kind === "project" && org ? { org } : {}) });
      onDone(kind === "pack" ? packScope(name) : kind === "org" ? orgScope(name) : projectScope(org!, name));
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="su-create" onSubmit={submit} aria-label={`Create ${kind}`}>
      <label className="su-field__label" htmlFor={`su-new-${kind}`}>
        New {kind}
        {kind === "project" && org ? ` in ${org}` : ""}
      </label>
      <input
        id={`su-new-${kind}`}
        className="su-input"
        value={name}
        onChange={(e) => setName(e.target.value.toLowerCase())}
        placeholder="name"
        autoFocus
        aria-invalid={bad}
        aria-describedby={bad ? `su-new-${kind}-err` : undefined}
        onKeyDown={(e) => e.key === "Escape" && onCancel()}
      />
      {bad && (
        <p className="su-field__error" id={`su-new-${kind}-err`}>
          Lowercase letters, digits, dash or underscore.
        </p>
      )}
      {error && <p className="su-field__error" role="alert">{error}</p>}
      <div className="su-create__actions">
        <button className="btn btn--primary" type="submit" disabled={!validName(name) || busy}>
          {busy ? "Creating…" : "Create"}
        </button>
        <button className="btn" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function ScopeTree({
  tree,
  loading,
  error,
  onRetry,
  selected,
  onSelect,
  onCreated,
}: {
  tree: Tree | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  selected: Scope;
  onSelect: (s: Scope) => void;
  onCreated: (s: Scope) => void;
}) {
  const [creating, setCreating] = useState<CreateKind | null>(null);
  // A project needs an org: the selected org, or the org of the selected project.
  const targetOrg = selected.org ?? null;

  return (
    <nav className="su-tree" aria-label="Setup scopes">
      <div className="su-tree__head">
        <h3 className="su-side-title">Scopes</h3>
      </div>
      {loading && !tree && <Loading what="scopes" />}
      {error && <ErrorBox message={error} onRetry={onRetry} />}
      {tree && (
        <ul role="tree" aria-label="Scope tree" className="su-tree__list">
          <Row scope={BASELINE} selected={selected.id === BASELINE.id} onSelect={onSelect} depth={0} hint="base layer" />
          <li role="none" className="su-tree__group">Packs</li>
          {tree.packs.length === 0 && <li role="none" className="su-tree__none">No packs yet</li>}
          {tree.packs.map((p) => (
            <Row key={p} scope={packScope(p)} selected={selected.id === packScope(p).id} onSelect={onSelect} depth={1} />
          ))}
          <li role="none" className="su-tree__group">Orgs</li>
          {tree.orgs.length === 0 && <li role="none" className="su-tree__none">No orgs yet</li>}
          {tree.orgs.map((o) => (
            <li role="none" key={o.name}>
              <ul role="group" className="su-tree__list">
                <Row
                  scope={orgScope(o.name)}
                  selected={selected.id === orgScope(o.name).id}
                  onSelect={onSelect}
                  depth={1}
                  hint={o.packs.length ? `packs: ${o.packs.join(", ")}` : undefined}
                />
                {o.projects.map((p) => (
                  <Row
                    key={p}
                    scope={projectScope(o.name, p)}
                    selected={selected.id === projectScope(o.name, p).id}
                    onSelect={onSelect}
                    depth={2}
                  />
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
      {tree && tree.packs.length + tree.orgs.length === 0 && (
        <Empty title="Only the baseline exists">Create a pack or an org to layer setups on top of it.</Empty>
      )}
      <div className="su-tree__actions">
        {creating ? (
          <CreateForm
            kind={creating}
            org={targetOrg}
            onCancel={() => setCreating(null)}
            onDone={(s) => {
              setCreating(null);
              onCreated(s);
            }}
          />
        ) : (
          <>
            <button className="btn" onClick={() => setCreating("pack")}>+ Pack</button>
            <button className="btn" onClick={() => setCreating("org")}>+ Org</button>
            <button
              className="btn"
              onClick={() => setCreating("project")}
              disabled={!targetOrg}
              title={targetOrg ? `Create a project in ${targetOrg}` : "Select an org or project first"}
            >
              + Project
            </button>
          </>
        )}
      </div>
    </nav>
  );
}
