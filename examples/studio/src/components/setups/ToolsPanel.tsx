import { useState, type FormEvent } from "react";
import {
  APPROVAL_CLASSES,
  buildDecl,
  EMPTY_REGISTER,
  errorText,
  scopeContext,
  setupsApi,
  validateRegister,
  type CatalogEntry,
  type ProbeResult,
  type RegisterForm,
  type Scope,
  type SetupItem,
  type SyncDiff,
  type ToolKind,
} from "../../lib/setups";
import { ApprovalBadge, ConfirmButton, Empty, ErrorBox, isInherited, LayerBadge, Loading, useLoad } from "./shared";

// ---- catalog table ----

export function CatalogTable({ entries }: { entries: CatalogEntry[] }) {
  const [q, setQ] = useState("");
  const shown = entries.filter((e) =>
    `${e.name} ${e.alias.join(" ")} ${e.title} ${e.source}`.toLowerCase().includes(q.trim().toLowerCase()),
  );
  return (
    <>
      <div className="su-field su-field--inline">
        <label className="su-field__label" htmlFor="su-cat-q">Filter</label>
        <input id="su-cat-q" className="su-input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="name, alias, title" />
        <span className="su-dim">{shown.length} of {entries.length}</span>
      </div>
      <div className="su-scroll-x">
        <table className="su-table" aria-label="Tool catalog">
          <thead>
            <tr>
              <th scope="col">Title</th>
              <th scope="col">Name</th>
              <th scope="col">Alias</th>
              <th scope="col">Approval</th>
              <th scope="col">Source</th>
              <th scope="col">Endpoint</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((e) => (
              <tr key={e.name}>
                <td>{e.title || <span className="su-dim">-</span>}</td>
                <th scope="row"><code>{e.name}</code></th>
                <td>{e.alias.length ? e.alias.map((a) => <code key={a} className="su-chip">{a}</code>) : <span className="su-dim">-</span>}</td>
                <td><ApprovalBadge approval={e.approval} /></td>
                <td><span className="su-dim">{e.source || "-"}</span></td>
                <td className="su-endpoint"><code>{e.endpoint || "-"}</code></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

// ---- register form ----

function Field({ id, label, children, hint }: { id: string; label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div className="su-field">
      <label className="su-field__label" htmlFor={id}>{label}</label>
      {children}
      {hint && <p className="su-hint">{hint}</p>}
    </div>
  );
}

export function RegisterTool({ scope, onDone, onClose }: { scope: Scope; onDone: () => void; onClose: () => void }) {
  const [f, setF] = useState<RegisterForm>(EMPTY_REGISTER);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reasons, setReasons] = useState<string[]>([]);
  const [touched, setTouched] = useState(false);
  const set = <K extends keyof RegisterForm>(k: K, v: RegisterForm[K]) => setF((x) => ({ ...x, [k]: v }));
  const problems = validateRegister(f);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (problems.length || busy) return;
    setBusy(true);
    setError(null);
    setReasons([]);
    try {
      await setupsApi.register(scope.id, buildDecl(f));
      setF(EMPTY_REGISTER);
      setTouched(false);
      onDone();
    } catch (err) {
      setError(errorText(err));
      setReasons((err as { reasons?: string[] }).reasons ?? []);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="su-import" onSubmit={submit} aria-label="Register a tool" noValidate>
      <header className="su-editor__head">
        <h3 className="su-h3">Register a tool in {scope.label}</h3>
        <button type="button" className="btn" onClick={onClose}>Close</button>
      </header>
      <div className="su-grid2">
        <Field id="su-t-kind" label="Kind">
          <select id="su-t-kind" className="su-input" value={f.kind} onChange={(e) => set("kind", e.target.value as ToolKind)}>
            <option value="http">http (one endpoint)</option>
            <option value="openapi">openapi (operations from a spec)</option>
            <option value="mcp">mcp (a tool server)</option>
          </select>
        </Field>
        <Field id="su-t-name" label="Name">
          <input id="su-t-name" className="su-input" value={f.name} onChange={(e) => set("name", e.target.value.toLowerCase())} placeholder="lookup_account" />
        </Field>
      </div>
      <Field id="su-t-desc" label="Description" hint="What the tool does and when an agent should call it.">
        <textarea id="su-t-desc" className="launcher__input" rows={2} value={f.description} onChange={(e) => set("description", e.target.value)} />
      </Field>

      {f.kind === "http" && (
        <div className="su-grid2 su-grid2--wide">
          <Field id="su-t-url" label="Endpoint URL">
            <input id="su-t-url" className="su-input" value={f.url} onChange={(e) => set("url", e.target.value)} placeholder="https://api.example.com/accounts" />
          </Field>
          <Field id="su-t-method" label="Method">
            <select id="su-t-method" className="su-input" value={f.method} onChange={(e) => set("method", e.target.value)}>
              {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => <option key={m}>{m}</option>)}
            </select>
          </Field>
        </div>
      )}
      {f.kind === "openapi" && (
        <>
          <div className="su-grid2">
            <Field id="su-t-spec" label="Spec URL or path">
              <input id="su-t-spec" className="su-input" value={f.spec} onChange={(e) => set("spec", e.target.value)} placeholder="https://api.example.com/openapi.json" />
            </Field>
            <Field id="su-t-base" label="Base URL (optional)">
              <input id="su-t-base" className="su-input" value={f.baseUrl} onChange={(e) => set("baseUrl", e.target.value)} />
            </Field>
          </div>
          <Field id="su-t-ops" label="Operation ids" hint="Comma separated. Only these operations become tools.">
            <input id="su-t-ops" className="su-input" value={f.operationIds} onChange={(e) => set("operationIds", e.target.value)} placeholder="listAccounts, getAccount" />
          </Field>
        </>
      )}
      {f.kind === "mcp" && (
        <div className="su-grid2 su-grid2--wide">
          <Field id="su-t-smode" label="Server">
            <select id="su-t-smode" className="su-input" value={f.serverMode} onChange={(e) => set("serverMode", e.target.value as "url" | "command")}>
              <option value="url">URL</option>
              <option value="command">Command</option>
            </select>
          </Field>
          {f.serverMode === "url" ? (
            <Field id="su-t-surl" label="Server URL">
              <input id="su-t-surl" className="su-input" value={f.serverUrl} onChange={(e) => set("serverUrl", e.target.value)} placeholder="https://mcp.example.com" />
            </Field>
          ) : (
            <Field id="su-t-scmd" label="Command">
              <input id="su-t-scmd" className="su-input" value={f.serverCommand} onChange={(e) => set("serverCommand", e.target.value)} placeholder="npx some-mcp-server" />
            </Field>
          )}
        </div>
      )}

      <div className="su-grid2">
        <Field id="su-t-appr" label="Approval class" hint="Leave on default to let the server choose from the method.">
          <select id="su-t-appr" className="su-input" value={f.approval} onChange={(e) => set("approval", e.target.value)}>
            <option value="">default</option>
            {APPROVAL_CLASSES.map((a) => <option key={a} value={a}>{a.replace("_", " ")}</option>)}
          </select>
        </Field>
        <Field id="su-t-secret" label="Auth secret reference" hint="A reference name like provider:handle. Never paste the secret.">
          <input id="su-t-secret" className="su-input" value={f.secretRef} onChange={(e) => set("secretRef", e.target.value)} placeholder="acme:api-key" autoComplete="off" spellCheck={false} />
        </Field>
      </div>

      {touched && problems.length > 0 && (
        <ul className="su-lint__list" role="alert">
          {problems.map((p) => (
            <li key={p} className="su-lint__item su-lint__item--error"><span className="su-lint__sev">fix</span><span className="su-lint__msg">{p}</span></li>
          ))}
        </ul>
      )}
      {error && <ErrorBox message={error} />}
      {reasons.length > 0 && (
        <ul className="su-lint__list" aria-label="Policy reasons">
          {reasons.map((r, i) => (
            <li key={i} className="su-lint__item su-lint__item--error"><span className="su-lint__sev">policy</span><span className="su-lint__msg">{r}</span></li>
          ))}
        </ul>
      )}
      <div className="su-actions">
        <button className="btn btn--primary" type="submit" disabled={busy}>{busy ? "Registering…" : "Register"}</button>
      </div>
    </form>
  );
}

// ---- sync and probe output ----

function DiffList({ label, names, kind }: { label: string; names: string[]; kind: "added" | "removed" | "changed" }) {
  return (
    <div className={`su-diff su-diff--${kind}`}>
      <h4 className="su-diff__h">{label} <span className="su-dim">{names.length}</span></h4>
      {names.length === 0 ? <p className="su-dim">none</p> : <ul>{names.map((n) => <li key={n}><code>{n}</code></li>)}</ul>}
    </div>
  );
}

export function SyncDiffView({ diff }: { diff: SyncDiff }) {
  return (
    <div className="su-diffs" role="status" aria-label="Sync diff">
      <DiffList label="Added" names={diff.added} kind="added" />
      <DiffList label="Removed" names={diff.removed} kind="removed" />
      <DiffList label="Changed" names={diff.changed} kind="changed" />
    </div>
  );
}

export function ProbeView({ r }: { r: ProbeResult }) {
  return (
    <div className="su-diffs" role="status" aria-label="Probe result">
      <div className="su-diff su-diff--added">
        <h4 className="su-diff__h">Probed <span className="su-dim">{r.probed.length}</span></h4>
        {r.probed.length === 0 ? <p className="su-dim">none</p> : <ul>{r.probed.map((n) => <li key={n}><code>{n}</code></li>)}</ul>}
      </div>
      <div className="su-diff su-diff--changed">
        <h4 className="su-diff__h">Skipped <span className="su-dim">{r.skipped.length}</span></h4>
        {r.skipped.length === 0 ? <p className="su-dim">none</p> : (
          <ul>{r.skipped.map((n) => <li key={n.name}><code>{n.name}</code> {n.reason && <span className="su-dim">{n.reason}</span>}</li>)}</ul>
        )}
      </div>
    </div>
  );
}

// ---- panel ----

export function ToolsPanel({ scope }: { scope: Scope }) {
  const ctx = scopeContext(scope);
  const cat = useLoad(() => setupsApi.catalog(ctx.org, ctx.project), scope.id);
  const reg = useLoad(() => setupsApi.items(scope.id, "tools"), scope.id);
  const [registering, setRegistering] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [diff, setDiff] = useState<SyncDiff | null>(null);
  const [probe, setProbe] = useState<ProbeResult | null>(null);

  const reloadAll = () => {
    cat.reload();
    reg.reload();
  };

  const act = async (id: string, fn: () => Promise<void>) => {
    setBusy(id);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  const toggle = (t: SetupItem) =>
    act(`toggle:${t.name}`, async () => {
      await setupsApi.enableTool(scope.id, t.name, !t.enabled);
      reloadAll();
    });
  const unregister = (t: SetupItem) =>
    act(`del:${t.name}`, async () => {
      await setupsApi.unregister(scope.id, t.name);
      reloadAll();
    });
  const sync = () =>
    act("sync", async () => {
      setDiff(await setupsApi.sync(scope.id));
      reloadAll();
    });
  const doProbe = () =>
    act("probe", async () => {
      setProbe(await setupsApi.probe(scope.id));
      cat.reload();
    });

  // The items listing may not include registered tools, so fold in the catalog's registered entries.
  const registered: SetupItem[] = (() => {
    const byName = new Map<string, SetupItem>();
    for (const e of cat.data ?? []) {
      if (e.origin === "registered")
        byName.set(e.name, { name: e.name, kind: "tools", source_layer: e.layer, size: null, updated_at: null, enabled: true });
    }
    for (const t of reg.data ?? []) byName.set(t.name, t);
    return [...byName.values()];
  })();

  return (
    <div className="su-panel">
      <div className="su-panel__bar">
        <p className="su-panel__sub">
          Tools visible in <strong>{scope.label}</strong>. Auth is shown as a secret reference name only.
        </p>
        <div className="su-actions">
          <button className="btn" onClick={sync} disabled={busy !== null}>{busy === "sync" ? "Syncing…" : "Sync"}</button>
          <button className="btn" onClick={doProbe} disabled={busy !== null} title="Calls safe GET operations only">
            {busy === "probe" ? "Probing…" : "Probe"}
          </button>
          <button className="btn btn--primary" onClick={() => setRegistering((v) => !v)} aria-expanded={registering}>Register tool</button>
        </div>
      </div>

      {error && <ErrorBox message={error} />}
      {diff && (
        <section aria-label="Sync result">
          <h3 className="su-h3">Sync diff</h3>
          <SyncDiffView diff={diff} />
        </section>
      )}
      {probe && (
        <section aria-label="Probe summary">
          <h3 className="su-h3">Probe (safe GETs only)</h3>
          <ProbeView r={probe} />
        </section>
      )}
      {registering && (
        <RegisterTool
          scope={scope}
          onClose={() => setRegistering(false)}
          onDone={() => {
            setRegistering(false);
            reloadAll();
          }}
        />
      )}

      <section aria-label="Registered tools">
        <h3 className="su-h3">Registered in this scope</h3>
        {reg.loading && !reg.data && <Loading what="registered tools" />}
        {reg.error && <ErrorBox message={reg.error} onRetry={reg.reload} />}
        {reg.data && cat.data && registered.length === 0 && (
          <Empty title="Nothing registered here">Use Register tool to add an http, openapi or mcp tool to this scope.</Empty>
        )}
        {registered.length > 0 && (
          <table className="su-table" aria-label="Registered tools">
            <thead>
              <tr><th scope="col">Name</th><th scope="col">Layer</th><th scope="col">State</th><th scope="col"><span className="su-sr">Actions</span></th></tr>
            </thead>
            <tbody>
              {registered.map((t) => (
                <tr key={t.name}>
                  <th scope="row"><code>{t.name}</code></th>
                  <td><LayerBadge source={t.source_layer} inherited={isInherited(t.source_layer, scope.id)} /></td>
                  <td>{t.enabled ? <span className="su-badge su-badge--on">enabled</span> : <span className="su-badge su-badge--off">disabled</span>}</td>
                  <td className="su-row-actions">
                    {isInherited(t.source_layer, scope.id) ? <span className="su-dim">inherited</span> : <>
                    <button className="btn" onClick={() => toggle(t)} disabled={busy !== null} aria-label={`${t.enabled ? "Disable" : "Enable"} ${t.name}`}>
                      {t.enabled ? "Disable" : "Enable"}
                    </button>
                    <ConfirmButton label="Unregister" confirmLabel={`Unregister ${t.name}`} onConfirm={() => unregister(t)} disabled={busy !== null} />
                    </>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section aria-label="Tool catalog">
        <h3 className="su-h3">Catalog</h3>
        {cat.loading && !cat.data && <Loading what="catalog" />}
        {cat.error && <ErrorBox message={cat.error} onRetry={cat.reload} />}
        {cat.data && cat.data.length === 0 && <Empty title="The catalog is empty">Register a tool, or sync sources declared in tools.yaml.</Empty>}
        {cat.data && cat.data.length > 0 && <CatalogTable entries={cat.data} />}
      </section>
    </div>
  );
}
