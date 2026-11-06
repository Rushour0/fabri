import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Plus, X } from "lucide-react";
import {
  connectionsApi,
  connectUrl,
  expiryLabel,
  type AttachReport,
  type Connection,
  type ConnectorInfo,
} from "../lib/connections";
import BrandMark, { type BrandId } from "./BrandMark";
import IntegrationSection from "./IntegrationSection";
import { ApprovalBadge, ConfirmButton } from "./setups/shared";
import "./setups/setups.css";

const VENDOR_MARKS: Record<string, BrandId> = { slack: "slack", github: "github", linear: "linear" };
const ACTOR = "${actor}";
const DEFAULT_HANDLE = "main";

const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));
const rowKey = (c: Connection) => `${c.provider}:${c.handle}`;

/** The connector a stored row belongs to (rows are keyed by the store provider). */
function connectorFor(row: Connection, connectors: ConnectorInfo[] | null): ConnectorInfo | undefined {
  return connectors?.find((c) => (c.store_provider ?? c.name) === row.provider) ?? connectors?.find((c) => c.name === row.provider);
}

/** A user connection resolves per run user, so a setup names it as ${actor}. */
const workspaceFor = (row: Connection) => (row.scope_kind === "user" ? ACTOR : row.handle);

function cliLine(connector: string, workspace: string, scope: string | null): string {
  const ws = workspace === ACTOR ? `'${ACTOR}'` : workspace;
  return `fabri setups tools add connector ${connector} --workspace ${ws}` + (scope ? ` --scope ${scope}` : "");
}

interface AttachState {
  open: boolean;
  scope: string;
  busy: boolean;
  report: AttachReport | null;
  error: string | null;
}

export default function ConnectionsPanel({
  locked,
  onRequireAuth,
  scopes,
  signedIn = true,
}: {
  locked: boolean;
  onRequireAuth: () => void;
  /** Setup scope ids (baseline, pack:x, org:x, org:x/project:y) a connection can attach to. */
  scopes: string[];
  /** False when auth is off: user connections need a signed-in user. */
  signedIn?: boolean;
}) {
  const [rows, setRows] = useState<Connection[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [connectors, setConnectors] = useState<ConnectorInfo[] | null>(null);
  const [connectorsFailed, setConnectorsFailed] = useState(false);
  const [banner, setBanner] = useState<{ provider: string; ok: boolean } | null>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [provider, setProvider] = useState("");
  const [scope, setScope] = useState<"org" | "user">("org");
  const [handle, setHandle] = useState(DEFAULT_HANDLE);
  // The token lives only in the input element: it is read on submit, sent
  // once, and the input is cleared. It never enters React state.
  const tokenRef = useRef<HTMLInputElement>(null);
  const [hasToken, setHasToken] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [formDone, setFormDone] = useState<string | null>(null);

  const [removeError, setRemoveError] = useState<Record<string, string>>({});
  const [attach, setAttach] = useState<Record<string, AttachState>>({});

  const refetch = useCallback(async () => {
    setListError(null);
    try {
      setRows(await connectionsApi.list());
    } catch (e) {
      setListError(errText(e));
    }
  }, []);

  useEffect(() => {
    // Signed-out visitors see the pitch, not a 401.
    if (locked) return;
    let live = true;
    void refetch();
    connectionsApi
      .connectors()
      .then((list) => {
        if (live) setConnectors(list);
      })
      .catch(() => {
        // An older server has no connector list; the add form stays hidden.
        if (live) setConnectorsFailed(true);
      });
    return () => {
      live = false;
    };
  }, [locked, refetch]);

  // The OAuth callback lands on /?connect=<provider>&status=connected#settings.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const p = params.get("connect");
    if (p === null) return;
    setBanner({ provider: p, ok: params.get("status") === "connected" });
    params.delete("connect");
    params.delete("status");
    const search = params.toString();
    window.history.replaceState({}, "", `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`);
  }, []);

  // Only connectors that store a credential can be added here.
  const addable = useMemo(() => (connectors ?? []).filter((c) => c.store_provider), [connectors]);
  const selected = addable.find((c) => c.name === provider);

  const pickProvider = (name: string) => {
    setProvider(name);
    setFormError(null);
    setFormDone(null);
    const c = addable.find((x) => x.name === name);
    setScope(c && c.scope_default === "user" && signedIn ? "user" : "org");
  };

  const clearToken = () => {
    if (tokenRef.current) tokenRef.current.value = "";
    setHasToken(false);
  };

  const submitToken = async (e: FormEvent) => {
    e.preventDefault();
    if (!selected) return;
    const token = tokenRef.current?.value.trim() ?? "";
    clearToken();
    if (!token) {
      setFormError("Paste a token first.");
      return;
    }
    setBusy(true);
    setFormError(null);
    setFormDone(null);
    try {
      const row = await connectionsApi.connect({
        provider: selected.name,
        token,
        scope,
        ...(scope === "org" ? { handle: handle.trim() || DEFAULT_HANDLE } : {}),
      });
      setFormDone(`Connected ${selected.title} as ${row.handle}.`);
      await refetch();
    } catch (err) {
      setFormError(errText(err));
    } finally {
      setBusy(false);
    }
  };

  const startOAuth = () => {
    if (!selected) return;
    window.location.assign(connectUrl(selected.name, scope));
  };

  const remove = async (row: Connection) => {
    const key = rowKey(row);
    setRemoveError(({ [key]: _, ...rest }) => rest);
    try {
      await connectionsApi.remove(row.provider, row.handle);
      setAttach(({ [key]: _, ...rest }) => rest);
      await refetch();
    } catch (e) {
      setRemoveError((m) => ({ ...m, [key]: errText(e) }));
    }
  };

  const patchAttach = (key: string, patch: Partial<AttachState>) =>
    setAttach((m) => {
      const cur = m[key] ?? { open: false, scope: scopes[0] ?? "", busy: false, report: null, error: null };
      return { ...m, [key]: { ...cur, ...patch } };
    });

  const runAttach = async (row: Connection, connector: string) => {
    const key = rowKey(row);
    const st = attach[key];
    const target = st?.scope || scopes[0];
    if (!target) return;
    patchAttach(key, { busy: true, error: null, report: null, scope: target });
    try {
      const report = await connectionsApi.attach(target, connector, workspaceFor(row));
      patchAttach(key, { busy: false, report });
    } catch (e) {
      patchAttach(key, { busy: false, error: errText(e) });
    }
  };

  const tokenMode = selected ? selected.auth_kind !== "oauth" : false;
  const oauthMode = selected ? selected.oauth : false;
  const userScopeBlocked = scope === "user" && !signedIn;

  const addForm = connectorsFailed ? (
    <p className="conn__note">This server does not list connectors, so connections can only be added with fabri connect.</p>
  ) : !connectors ? (
    <p className="conn__note">Loading connectors…</p>
  ) : (
    <form className="conn__form" onSubmit={submitToken} aria-label="Add connection">
      <div className="conn__form-row">
        <label className="su-field">
          <span className="su-field__label">App</span>
          <select className="su-input" value={provider} onChange={(e) => pickProvider(e.target.value)}>
            <option value="">Choose an app</option>
            {addable.map((c) => (
              <option key={c.name} value={c.name}>
                {c.title}
                {c.experimental ? " (experimental)" : ""}
              </option>
            ))}
          </select>
        </label>
        {selected && (
          <label className="su-field">
            <span className="su-field__label">Who can use it</span>
            <select
              className="su-input"
              value={scope}
              onChange={(e) => setScope(e.target.value === "user" ? "user" : "org")}
            >
              <option value="org">Everyone in the org</option>
              <option value="user" disabled={!signedIn}>
                Only me
              </option>
            </select>
          </label>
        )}
        {selected && scope === "org" && (
          <label className="su-field">
            <span className="su-field__label">Name</span>
            <input
              className="su-input conn__mono"
              value={handle}
              onChange={(e) => setHandle(e.target.value)}
              placeholder={DEFAULT_HANDLE}
              spellCheck={false}
              autoComplete="off"
            />
          </label>
        )}
      </div>

      {selected && (
        <>
          <p className="conn__summary">
            {selected.summary}
            {selected.curated && <span className="su-badge conn__badge">curated tools</span>}
            {selected.experimental && <span className="su-badge conn__badge conn__badge--exp">experimental</span>}
          </p>
          {scope === "user" && (
            <p className="su-hint">
              {signedIn
                ? "Stored under your account. Agents running for you use it; nobody else's runs can."
                : "Only me needs a signed-in user, and auth is off on this server."}
            </p>
          )}

          {oauthMode && (
            <div className="conn__oauth">
              <button
                type="button"
                className="btn btn--primary"
                onClick={startOAuth}
                disabled={!selected.oauth_configured || userScopeBlocked}
              >
                Connect with {selected.title}
              </button>
              {!selected.oauth_configured && (
                <p className="su-hint">
                  Sign-in with {selected.title} is not set up on this server. It needs the app's client id and
                  secret and FABRI_PUBLIC_BASE_URL.
                  {selected.oauth_loopback && <> On your own machine, run <code>fabri connect {selected.name}</code> instead.</>}
                  {tokenMode && " You can also paste a token below."}
                </p>
              )}
            </div>
          )}

          {tokenMode && (
            <div className="conn__token">
              <label className="su-field">
                <span className="su-field__label">{selected.auth_kind === "api_key" ? "API key" : "Token"}</span>
                <input
                  ref={tokenRef}
                  className="su-input conn__mono"
                  type="password"
                  name="token"
                  autoComplete="off"
                  spellCheck={false}
                  placeholder="Paste it here. It is not shown again."
                  onChange={(e) => setHasToken(e.target.value.length > 0)}
                />
              </label>
              <button type="submit" className="btn" disabled={busy || !hasToken || userScopeBlocked}>
                {busy ? "Checking…" : "Verify and save"}
              </button>
            </div>
          )}
          {tokenMode && (
            <p className="su-hint">fabri checks the token with {selected.title} before it saves it.</p>
          )}
        </>
      )}
      {formError && <p className="su-field__error" role="alert">{formError}</p>}
      {formDone && <p className="conn__ok" role="status">{formDone}</p>}
    </form>
  );

  return (
    <div className="conn">
      <IntegrationSection
        name="Connections"
        blurb="Connect an app once, then give its tools to any setup. Tokens are stored on this server and never shown again; agents use them through the approval rules you set."
        brand="connections"
        hue="#9497a3"
        handoff={["Connect an app", "Attach it to a setup", "Agents call it, with approval"]}
        locked={locked}
        onRequireAuth={onRequireAuth}
        connect={
          <button
            type="button"
            className="integration__cta"
            aria-expanded={formOpen}
            onClick={() => setFormOpen((v) => !v)}
          >
            {formOpen ? <X size={13} strokeWidth={2} aria-hidden /> : <Plus size={13} strokeWidth={2} aria-hidden />}
            {formOpen ? "Close" : "Add connection"}
          </button>
        }
      >
        {banner && (
          <div className={banner.ok ? "conn__ok conn__banner" : "error-banner"} role="status">
            {banner.ok ? `${banner.provider} connected.` : `Could not connect ${banner.provider}. Try again, or check the server log.`}
          </div>
        )}
        {formOpen && addForm}

        {listError ? (
          <div className="error-banner">{listError}</div>
        ) : rows === null ? (
          <p>Loading connections…</p>
        ) : rows.length === 0 ? (
          <p>No connections yet.</p>
        ) : (
          <ul className="conn__list" aria-label="Connections">
            {rows.map((row) => {
              const key = rowKey(row);
              const info = connectorFor(row, connectors);
              const connector = info?.name ?? row.provider;
              const expiry = expiryLabel(row.expires_at);
              const st = attach[key];
              const ws = workspaceFor(row);
              return (
                <li className="conn__row" key={key}>
                  <div className="conn__main">
                    <span className="conn__mark" aria-hidden>
                      <BrandMark brand={VENDOR_MARKS[connector] ?? "connections"} size={14} />
                    </span>
                    <div className="conn__who">
                      <span className="conn__title">
                        {info?.title ?? row.provider}
                        <code className="conn__handle">{row.scope_kind === "user" ? (row.mine ? "you" : row.handle) : row.handle}</code>
                      </span>
                      <span className="conn__meta">
                        <span className={"su-badge" + (row.scope_kind === "org" ? " su-badge--org" : "")}>
                          {row.scope_kind === "org" ? "org" : row.mine ? "only you" : "user"}
                        </span>
                        {info?.experimental && <span className="su-badge conn__badge--exp">experimental</span>}
                        {row.status !== "ok" && row.status !== "active" && <span className="su-badge conn__badge--warn">{row.status}</span>}
                        {row.display_name && <span className="conn__dim">{row.display_name}</span>}
                        <span className={"conn__dim" + (expiry === "expired" ? " conn__dim--err" : "")}>{expiry}</span>
                      </span>
                    </div>
                    <div className="conn__actions">
                      <button
                        type="button"
                        className="btn"
                        aria-expanded={!!st?.open}
                        onClick={() => patchAttach(key, { open: !st?.open })}
                      >
                        Attach to setup
                      </button>
                      <ConfirmButton label="Remove" confirmLabel="Remove connection" onConfirm={() => void remove(row)} />
                    </div>
                  </div>
                  {removeError[key] && <p className="su-field__error" role="alert">{removeError[key]}</p>}

                  {st?.open && (
                    <div className="conn__attach">
                      {scopes.length > 0 && (
                        <div className="conn__attach-row">
                          <label className="su-field">
                            <span className="su-field__label">Setup scope</span>
                            <select
                              className="su-input conn__mono"
                              value={st.scope || scopes[0]}
                              onChange={(e) => patchAttach(key, { scope: e.target.value, report: null, error: null })}
                            >
                              {scopes.map((s) => (
                                <option key={s} value={s}>
                                  {s}
                                </option>
                              ))}
                            </select>
                          </label>
                          <button
                            type="button"
                            className="btn btn--primary"
                            disabled={st.busy}
                            onClick={() => void runAttach(row, connector)}
                          >
                            {st.busy ? "Attaching…" : "Attach"}
                          </button>
                        </div>
                      )}
                      <p className="su-hint">
                        {scopes.length > 0 ? "Or from a terminal:" : "No setup scopes on this server. From a terminal:"}
                      </p>
                      <code className="conn__cli">{cliLine(connector, ws, scopes.length > 0 ? st.scope || scopes[0] : null)}</code>
                      {row.scope_kind === "user" && (
                        <p className="su-hint">{ACTOR} means each run uses the connection of the person it runs for.</p>
                      )}
                      {st.error && <p className="su-field__error conn__verbatim" role="alert">{st.error}</p>}
                      {st.report && (
                        <div className="conn__report" role="status">
                          <p className="conn__ok">
                            {st.report.written ? "Added" : "Checked"} {st.report.name || connector}: {st.report.tool_count} tool
                            {st.report.tool_count === 1 ? "" : "s"}
                            {st.report.file ? ` in ${st.report.file}` : ""}.
                          </p>
                          {st.report.tools.length > 0 && (
                            <ul className="conn__tools">
                              {st.report.tools.map((t) => (
                                <li key={t.name}>
                                  <code>{t.name}</code>
                                  <ApprovalBadge approval={t.approval} />
                                </li>
                              ))}
                            </ul>
                          )}
                          {st.report.warnings.map((w, i) => (
                            <p className="su-hint conn__verbatim" key={i}>
                              {w}
                            </p>
                          ))}
                          {st.report.errors.map((w, i) => (
                            <p className="su-field__error conn__verbatim" key={i}>
                              {w}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </IntegrationSection>
    </div>
  );
}
