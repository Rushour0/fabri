import { useEffect, useMemo, useState, type FormEvent } from "react";
import {
  errorText,
  fileTag,
  KIND_LABEL,
  KIND_SINGULAR,
  lintFor,
  setupsApi,
  templateFiles,
  validName,
  type Kind,
  type LintFinding,
  type Scope,
  type SetupItem,
} from "../../lib/setups";
import { ImportPanel } from "./ImportPanel";
import { ConfirmButton, Empty, ErrorBox, isInherited, LayerBadge, LintList, Loading, useLoad } from "./shared";

const DATA_NAME = /^[a-z0-9][a-z0-9-]*\.[a-z0-9]{1,8}$/;

function nameOk(kind: Kind, name: string): boolean {
  return kind === "data" ? DATA_NAME.test(name) : validName(name);
}

function fmtSize(n: number | null): string {
  if (n == null) return "";
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

function fmtTime(s: string | null): string {
  if (!s) return "";
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? s : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

// ---- editor for one item ----

export function Editor({
  scope,
  kind,
  name,
  item,
  onChanged,
  onClosed,
}: {
  scope: Scope;
  kind: Kind;
  name: string;
  item: SetupItem | undefined;
  onChanged: () => void;
  onClosed: () => void;
}) {
  const detail = useLoad(() => setupsApi.item(scope.id, kind, name), `${scope.id}|${kind}|${name}`);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState<Record<string, string>>({});
  const [lint, setLint] = useState<LintFinding[]>([]);
  const [busy, setBusy] = useState<null | "save" | "toggle" | "delete">(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (detail.data) {
      setFiles(detail.data.files);
      setSaved(detail.data.files);
      setLint(detail.data.lint);
      setError(null);
      setNote(null);
    }
  }, [detail.data]);

  const dirty = useMemo(() => Object.keys(files).some((k) => files[k] !== saved[k]), [files, saved]);
  const enabled = item?.enabled !== false;
  const inherited = isInherited(item?.source_layer ?? "", scope.id);

  const run = async (what: "save" | "toggle" | "delete", fn: () => Promise<void>) => {
    setBusy(what);
    setError(null);
    setNote(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const save = () =>
    run("save", async () => {
      const result = await setupsApi.putItem(scope.id, kind, name, files);
      setSaved(files);
      setLint(result);
      setNote("Saved. Live on the next resolve.");
      onChanged();
    });

  const toggle = () =>
    run("toggle", async () => {
      await setupsApi.disable(scope.id, kind, name, enabled);
      setNote(enabled ? "Disabled." : "Enabled.");
      onChanged();
    });

  const remove = () =>
    run("delete", async () => {
      await setupsApi.deleteItem(scope.id, kind, name);
      onChanged();
      onClosed();
    });

  const paths = Object.keys(files);

  return (
    <section className="su-editor" aria-label={`Editor for ${name}`}>
      <header className="su-editor__head">
        <div className="su-editor__title">
          <h3 className="su-h3">{name}</h3>
          {item && <LayerBadge source={item.source_layer} inherited={inherited} />}
          {!enabled && <span className="su-badge su-badge--off">disabled</span>}
          {dirty && <span className="su-badge su-badge--dirty">unsaved</span>}
        </div>
        <button className="btn su-editor__close" onClick={onClosed} aria-label="Close editor">
          Close
        </button>
      </header>

      {inherited && (
        <p className="su-note">
          This {KIND_SINGULAR[kind]} comes from {item?.source_layer}. Saving writes an override in {scope.label}.
        </p>
      )}
      {detail.loading && <Loading what={name} />}
      {detail.error && <ErrorBox message={detail.error} onRetry={detail.reload} />}

      {detail.data && (
        <>
          {paths.length === 0 && <Empty title="No files in this item" />}
          {paths.map((p) => (
            <div className="su-file" key={p}>
              <div className="su-file__head">
                <label className="su-file__path" htmlFor={`su-file-${p}`}>
                  <code>{p}</code>
                </label>
                <span className="su-tag">{fileTag(p, kind)}</span>
              </div>
              <textarea
                id={`su-file-${p}`}
                className="launcher__input su-code"
                value={files[p]}
                onChange={(e) => setFiles((f) => ({ ...f, [p]: e.target.value }))}
                rows={Math.min(22, Math.max(6, files[p].split("\n").length + 1))}
                spellCheck={fileTag(p, kind) === "Markdown"}
                wrap="off"
              />
            </div>
          ))}

          <LintList findings={lint.length ? lint : []} label="Lint" />
          {error && <ErrorBox message={error} />}
          {note && !error && (
            <p className="su-note su-note--ok" role="status">
              {note}
            </p>
          )}

          <div className="su-actions">
            <button className="btn btn--primary" onClick={save} disabled={!dirty || busy !== null}>
              {busy === "save" ? "Saving…" : "Save"}
            </button>
            <button className="btn" onClick={() => setFiles(saved)} disabled={!dirty || busy !== null}>
              Revert
            </button>
            <span className="su-actions__spacer" />
            <button className="btn" onClick={toggle} disabled={busy !== null || !item} aria-pressed={!enabled}>
              {busy === "toggle" ? "Working…" : enabled ? "Disable" : "Enable"}
            </button>
            <ConfirmButton label="Delete" confirmLabel={`Delete ${name}`} onConfirm={remove} disabled={busy !== null} />
          </div>
        </>
      )}
    </section>
  );
}

// ---- new item ----

export function NewItem({
  scope,
  kind,
  existing,
  onCreated,
  onCancel,
  makeFiles,
}: {
  scope: Scope;
  kind: Kind;
  existing: Set<string>;
  makeFiles?: (name: string) => Record<string, string>;
  onCreated: (name: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [text, setText] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bad = name !== "" && !nameOk(kind, name);
  const dup = existing.has(name);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!nameOk(kind, name) || dup || busy) return;
    setBusy(true);
    setError(null);
    try {
      const files = makeFiles ? makeFiles(name) : templateFiles(kind, name);
      if (text !== null) files[Object.keys(files)[0]] = text;
      await setupsApi.putItem(scope.id, kind, name, files);
      onCreated(name);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    setText(await f.text());
    if (!name) setName(f.name.toLowerCase().replace(/[^a-z0-9.-]+/g, "-"));
  };

  return (
    <form className="su-new" onSubmit={submit} aria-label={`New ${KIND_SINGULAR[kind]}`}>
      <label className="su-field__label" htmlFor="su-new-item">
        New {KIND_SINGULAR[kind]} in {scope.label}
      </label>
      <input
        id="su-new-item"
        className="su-input"
        value={name}
        onChange={(e) => setName(e.target.value.toLowerCase())}
        placeholder={kind === "data" ? "customers.json" : "name"}
        autoFocus
        aria-invalid={bad || dup}
      />
      {bad && (
        <p className="su-field__error">
          {kind === "data" ? "Use name.ext, for example customers.json." : "Lowercase letters, digits, dash or underscore."}
        </p>
      )}
      {dup && <p className="su-field__error">That name exists in this scope. Open it from the list.</p>}
      {(kind === "data" || kind === "context") && (
        <label className="su-upload">
          <span className="su-field__label">Or start from a text file</span>
          <input type="file" onChange={(e) => onFile(e.target.files?.[0])} />
        </label>
      )}
      {error && <ErrorBox message={error} />}
      <div className="su-create__actions">
        <button className="btn btn--primary" type="submit" disabled={!nameOk(kind, name) || dup || busy}>
          {busy ? "Creating…" : "Create"}
        </button>
        <button className="btn" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

// ---- panel ----

export function ItemsPanel({ scope, kind, localFirst = false }: { scope: Scope; kind: Exclude<Kind, "tools">; localFirst?: boolean }) {
  const items = useLoad(() => setupsApi.items(scope.id, kind), `${scope.id}|${kind}`);
  const lintAll = useLoad(() => setupsApi.lint(scope.id), scope.id);
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);
  // Project views start on what the project owns; inherited items are one click away.
  const [showInherited, setShowInherited] = useState(!localFirst);

  useEffect(() => {
    setOpen(null);
    setCreating(false);
    setImporting(false);
  }, [scope.id, kind]);

  const all = items.data ?? [];
  const hidden = showInherited ? 0 : all.filter((i) => isInherited(i.source_layer, scope.id)).length;
  const list = showInherited ? all : all.filter((i) => !isInherited(i.source_layer, scope.id));
  const existing = useMemo(() => new Set(all.map((i) => i.name)), [all]);
  const openItem = list.find((i) => i.name === open);

  return (
    <div className="su-panel">
      <div className="su-panel__bar">
        <p className="su-panel__sub">
          {KIND_LABEL[kind]} visible in <strong>{scope.label}</strong>. The badge shows the layer each one comes from.
        </p>
        <div className="su-actions">
          {localFirst && (
            <label className="su-check">
              <input type="checkbox" checked={showInherited} onChange={(e) => setShowInherited(e.target.checked)} />
              <span>Show inherited</span>
            </label>
          )}
          {kind === "skills" && (
            <button className="btn" onClick={() => { setImporting((v) => !v); setCreating(false); }} aria-expanded={importing}>
              Import
            </button>
          )}
          <button className="btn btn--primary" onClick={() => { setCreating((v) => !v); setImporting(false); }} aria-expanded={creating}>
            New {KIND_SINGULAR[kind]}
          </button>
        </div>
      </div>

      {importing && (
        <ImportPanel
          scope={scope}
          onApplied={() => {
            items.reload();
            lintAll.reload();
          }}
          onClose={() => setImporting(false)}
        />
      )}
      {creating && (
        <NewItem
          scope={scope}
          kind={kind}
          existing={existing}
          onCancel={() => setCreating(false)}
          onCreated={(n) => {
            setCreating(false);
            items.reload();
            lintAll.reload();
            setOpen(n);
          }}
        />
      )}

      <div className="su-split">
        <div className="su-list-wrap">
          {items.loading && !items.data && <Loading what={KIND_LABEL[kind].toLowerCase()} />}
          {items.error && <ErrorBox message={items.error} onRetry={items.reload} />}
          {items.data && list.length === 0 && (
            <Empty title={`No ${KIND_LABEL[kind].toLowerCase()} here yet`}>
              {hidden > 0 && `${hidden} inherited item${hidden === 1 ? " is" : "s are"} hidden. `}
              Create one{kind === "skills" ? " or import SKILL.md files" : ""}. Items from lower layers show up once they exist.
            </Empty>
          )}
          {list.length > 0 && (
            <table className="su-table" aria-label={`${KIND_LABEL[kind]} in ${scope.label}`}>
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col">Layer</th>
                  <th scope="col">Lint</th>
                  <th scope="col" className="su-num">Size</th>
                </tr>
              </thead>
              <tbody>
                {list.map((it) => {
                  const lf = lintFor(lintAll.data ?? [], kind, it.name);
                  const errs = lf.filter((l) => l.severity === "error").length;
                  return (
                    <tr key={it.name} className={it.name === open ? "su-row--on" : undefined}>
                      <th scope="row">
                        <button
                          className="su-link"
                          onClick={() => setOpen(it.name === open ? null : it.name)}
                          aria-expanded={it.name === open}
                        >
                          {it.name}
                        </button>
                        {!it.enabled && <span className="su-badge su-badge--off">disabled</span>}
                      </th>
                      <td>
                        <LayerBadge source={it.source_layer} inherited={isInherited(it.source_layer, scope.id)} />
                      </td>
                      <td>
                        {lf.length === 0 ? (
                          <span className="su-dim">ok</span>
                        ) : (
                          <span className={errs ? "su-lint-count su-lint-count--err" : "su-lint-count"}>
                            {lf.length} {errs ? "error" : "warning"}
                            {lf.length === 1 ? "" : "s"}
                          </span>
                        )}
                      </td>
                      <td className="su-num su-dim" title={fmtTime(it.updated_at)}>
                        {fmtSize(it.size)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          {lintAll.data && lintAll.data.length > 0 && !open && (
            <details className="su-details">
              <summary>All lint findings for {scope.label} ({lintAll.data.length})</summary>
              <LintList findings={lintAll.data} label="Scope lint" />
            </details>
          )}
        </div>

        {open && (
          <Editor
            key={`${scope.id}|${kind}|${open}`}
            scope={scope}
            kind={kind}
            name={open}
            item={openItem}
            onClosed={() => setOpen(null)}
            onChanged={() => {
              items.reload();
              lintAll.reload();
            }}
          />
        )}
      </div>
    </div>
  );
}
