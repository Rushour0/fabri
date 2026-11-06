// Client for the connections API (/connections, /connect/<provider>) and the
// connector list (/tools/connectors). Rows never carry a token; the only time
// a token is in this file is as a request body field, sent once.

import { notifyUnauthorized } from "./api";

export interface Connection {
  provider: string;
  handle: string;
  scope_kind: "org" | "user";
  display_name: string | null;
  status: string;
  scopes: string | null;
  expires_at: number | null;
  installed_at: number | null;
  mine: boolean;
}

export interface ConnectorInfo {
  name: string;
  title: string;
  summary: string;
  auth_kind: string;
  store_provider: string | null;
  scope_default: "org" | "user";
  oauth: boolean;
  oauth_configured: boolean;
  oauth_loopback: boolean;
  curated: boolean;
  experimental: boolean;
}

export interface AttachedTool {
  name: string;
  approval: string;
  kind: string;
}

/** What POST /tools/sources returns for a connector attach. */
export interface AttachReport {
  name: string;
  file: string;
  tool_count: number;
  tools: AttachedTool[];
  hidden: string[];
  warnings: string[];
  errors: string[];
  connected: boolean;
  written: boolean;
}

export class ConnectionsError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.name = "ConnectionsError";
    this.status = status;
  }
}

// ---- normalizers ----

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const str = (v: unknown): string | null => (typeof v === "string" && v !== "" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** Accepts `{connections: [...]}` or a bare array. Drops rows without a provider or handle. */
export function normConnections(raw: unknown): Connection[] {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw.connections) ? raw.connections : [];
  const out: Connection[] = [];
  for (const item of list) {
    if (!isObj(item)) continue;
    const provider = str(item.provider);
    const handle = str(item.handle);
    if (!provider || !handle) continue;
    out.push({
      provider,
      handle,
      scope_kind: item.scope_kind === "user" ? "user" : "org",
      display_name: str(item.display_name),
      status: str(item.status) ?? "active",
      scopes: str(item.scopes),
      expires_at: num(item.expires_at),
      installed_at: num(item.installed_at),
      mine: item.mine === true,
    });
  }
  return out;
}

/** Accepts a bare array or `{connectors: [...]}`. Drops entries without a name. */
export function normConnectors(raw: unknown): ConnectorInfo[] {
  const list = Array.isArray(raw) ? raw : isObj(raw) && Array.isArray(raw.connectors) ? raw.connectors : [];
  const out: ConnectorInfo[] = [];
  for (const item of list) {
    if (!isObj(item)) continue;
    const name = str(item.name);
    if (!name) continue;
    out.push({
      name,
      title: str(item.title) ?? name,
      summary: str(item.summary) ?? "",
      auth_kind: str(item.auth_kind) ?? "token",
      store_provider: str(item.store_provider),
      scope_default: item.scope_default === "user" ? "user" : "org",
      oauth: item.oauth === true,
      oauth_configured: item.oauth_configured === true,
      oauth_loopback: item.oauth_loopback === true,
      curated: item.curated === true,
      experimental: item.experimental === true,
    });
  }
  return out;
}

export function normAttachReport(raw: unknown): AttachReport {
  const b = isObj(raw) ? raw : {};
  const strs = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const tools: AttachedTool[] = [];
  if (Array.isArray(b.tools)) {
    for (const t of b.tools) {
      if (!isObj(t)) continue;
      const name = str(t.name);
      if (name) tools.push({ name, approval: str(t.approval) ?? "", kind: str(t.kind) ?? "" });
    }
  }
  return {
    name: str(b.name) ?? "",
    file: str(b.file) ?? "",
    tool_count: num(b.tool_count) ?? tools.length,
    tools,
    hidden: strs(b.hidden),
    warnings: strs(b.warnings),
    errors: strs(b.errors),
    connected: b.connected !== false,
    written: b.written === true,
  };
}

// ---- helpers ----

/** The URL that starts an OAuth flow. Open it in a new window. */
export function connectUrl(provider: string, scope: "org" | "user"): string {
  return `/connect/${encodeURIComponent(provider)}?scope=${scope}`;
}

const DAY = 86400;

/** "no expiry", "expired", or "expires in 3 d" / "expires in 5 h" / "expires in 10 min". `now` is epoch seconds. */
export function expiryLabel(expires_at: number | null, now: number = Math.floor(Date.now() / 1000)): string {
  if (expires_at === null) return "no expiry";
  const left = expires_at - now;
  if (left <= 0) return "expired";
  if (left >= DAY) return `expires in ${Math.floor(left / DAY)} d`;
  if (left >= 3600) return `expires in ${Math.floor(left / 3600)} h`;
  return `expires in ${Math.max(1, Math.floor(left / 60))} min`;
}

// ---- HTTP ----

async function call<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, credentials: "same-origin" });
  } catch (e) {
    throw new ConnectionsError(`Cannot reach the server (${e instanceof Error ? e.message : String(e)}).`);
  }
  if (res.status === 401) notifyUnauthorized();
  const text = await res.text();
  let body: unknown = undefined;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (!res.ok) {
    const b = isObj(body) ? body : {};
    const msg =
      res.status === 401
        ? "Sign in to manage connections."
        : [
            typeof b.error === "string" ? b.error : "",
            Array.isArray(b.reasons) ? b.reasons.map(String).join("; ") : "",
          ]
            .filter(Boolean)
            .join(": ") || text.slice(0, 200) || `request failed (${res.status})`;
    throw new ConnectionsError(msg, res.status);
  }
  return body as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

export const connectionsApi = {
  async list(): Promise<Connection[]> {
    return normConnections(await call("/connections"));
  },
  async connectors(): Promise<ConnectorInfo[]> {
    return normConnectors(await call("/tools/connectors"));
  },
  /** Stores a pasted token. The caller clears its input right after this returns or throws. */
  async connect(body: { provider: string; handle?: string; token: string; scope?: "org" | "user" }): Promise<Connection> {
    const raw = await call<unknown>("/connections", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify(body),
    });
    const rows = normConnections({ connections: isObj(raw) ? [raw.connection] : [] });
    if (!rows.length) throw new ConnectionsError("The server did not return the new connection.");
    return rows[0];
  },
  async remove(provider: string, handle: string): Promise<void> {
    await call(`/connections?provider=${encodeURIComponent(provider)}&handle=${encodeURIComponent(handle)}`, {
      method: "DELETE",
    });
  },
  /** Adds the connector as a tool source in a setup scope, using this connection's handle. */
  async attach(scope: string, provider: string, handle: string, name?: string): Promise<AttachReport> {
    const body: Record<string, unknown> = { source: "connector", connector: provider, workspace: handle, scope };
    if (name) body.name = name;
    return normAttachReport(
      await call("/tools/sources", { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) }),
    );
  },
};
