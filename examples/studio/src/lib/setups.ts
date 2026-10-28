// Client for the Setups API (docs/setups-api.md) plus the pure helpers the
// Setups views share: scope strings, response normalizers, new-item templates.
//
// The contract pins the routes and the main fields. Some response bodies
// ("the merged setup", "catalog entries") are described loosely, so every
// normalizer below accepts the plausible shapes (array or name-keyed map,
// string or list for aliases) and returns one stable shape for the views.

import { notifyUnauthorized } from "./api";

export const KINDS = ["agents", "skills", "context", "data", "tools", "plays"] as const;
export type Kind = (typeof KINDS)[number];

export const KIND_LABEL: Record<Kind, string> = {
  agents: "Agents",
  skills: "Skills",
  context: "Context",
  data: "Data",
  tools: "Tools",
  plays: "Plays",
};

export const KIND_SINGULAR: Record<Kind, string> = {
  agents: "agent type",
  skills: "skill",
  context: "context file",
  data: "data file",
  tools: "tool",
  plays: "play",
};

// ---- errors ----

export class SetupsError extends Error {
  status: number;
  reasons: string[];
  constructor(message: string, status = 0, reasons: string[] = []) {
    super(message);
    this.name = "SetupsError";
    this.status = status;
    this.reasons = reasons;
  }
}

export function errorText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

// ---- scopes ----

export type ScopeLevel = "baseline" | "pack" | "org" | "project";

export interface Scope {
  /** The scope string the API takes: baseline | pack:x | org:x | org:x/project:y */
  id: string;
  level: ScopeLevel;
  label: string;
  org?: string;
  project?: string;
}

export const BASELINE: Scope = { id: "baseline", level: "baseline", label: "Baseline" };

export function packScope(name: string): Scope {
  return { id: `pack:${name}`, level: "pack", label: name };
}
export function orgScope(org: string): Scope {
  return { id: `org:${org}`, level: "org", label: org, org };
}
export function projectScope(org: string, project: string): Scope {
  return { id: `org:${org}/project:${project}`, level: "project", label: project, org, project };
}

export function scopeLevelLabel(level: ScopeLevel): string {
  return { baseline: "baseline", pack: "pack", org: "org", project: "project" }[level];
}

const NAME_RE = /^[a-z0-9][a-z0-9_-]{0,62}$/;
/** Lowercase letters, digits, dash and underscore; no slashes or dots. */
export function validName(name: string): boolean {
  return NAME_RE.test(name);
}

export interface ScopeTree {
  baseline: Record<string, unknown>;
  packs: string[];
  orgs: { name: string; packs: string[]; projects: string[] }[];
}

export function normScopes(raw: unknown): ScopeTree {
  const r = (raw ?? {}) as Record<string, unknown>;
  const names = (v: unknown): string[] =>
    Array.isArray(v) ? v.map((x) => (typeof x === "string" ? x : String((x as { name?: string })?.name ?? ""))).filter(Boolean) : [];
  const orgs = Array.isArray(r.orgs)
    ? (r.orgs as unknown[]).map((o) => {
        const x = (o ?? {}) as Record<string, unknown>;
        return { name: String(x.name ?? ""), packs: names(x.packs), projects: names(x.projects) };
      })
    : [];
  return {
    baseline: (r.baseline as Record<string, unknown>) ?? {},
    packs: names(r.packs),
    orgs: orgs.filter((o) => o.name),
  };
}

export function allScopes(tree: ScopeTree): Scope[] {
  return [
    BASELINE,
    ...tree.packs.map(packScope),
    ...tree.orgs.flatMap((o) => [orgScope(o.name), ...o.projects.map((p) => projectScope(o.name, p))]),
  ];
}

// ---- items ----

export interface SetupItem {
  name: string;
  kind: string;
  source_layer: string;
  size: number | null;
  updated_at: string | null;
  enabled: boolean;
}

export function normItems(raw: unknown): SetupItem[] {
  const list = Array.isArray(raw) ? raw : Array.isArray((raw as { items?: unknown })?.items) ? (raw as { items: unknown[] }).items : [];
  return list.map((x) => {
    const r = (x ?? {}) as Record<string, unknown>;
    return {
      name: String(r.name ?? ""),
      kind: String(r.kind ?? ""),
      source_layer: String(r.source_layer ?? r.source ?? r.layer ?? ""),
      size: typeof r.size === "number" ? r.size : null,
      updated_at: typeof r.updated_at === "string" ? r.updated_at : null,
      enabled: r.enabled !== false,
    };
  });
}

export interface LintFinding {
  severity: "error" | "warning" | "info";
  kind: string;
  name: string;
  message: string;
}

export function normLint(raw: unknown): LintFinding[] {
  const list = Array.isArray(raw) ? raw : Array.isArray((raw as { lint?: unknown })?.lint) ? (raw as { lint: unknown[] }).lint : [];
  return list.map((x) => {
    if (typeof x === "string") return { severity: "warning", kind: "", name: "", message: x };
    const r = (x ?? {}) as Record<string, unknown>;
    const sev = String(r.severity ?? r.level ?? "warning");
    return {
      severity: sev === "error" ? "error" : sev === "info" ? "info" : "warning",
      kind: String(r.kind ?? ""),
      name: String(r.name ?? r.item ?? ""),
      message: String(r.message ?? ""),
    };
  });
}

export interface ItemDetail {
  name: string;
  kind: string;
  files: Record<string, string>;
  meta: Record<string, unknown>;
  lint: LintFinding[];
}

export function normItem(raw: unknown): ItemDetail {
  const r = (raw ?? {}) as Record<string, unknown>;
  const files: Record<string, string> = {};
  for (const [k, v] of Object.entries((r.files as Record<string, unknown>) ?? {})) files[k] = typeof v === "string" ? v : "";
  return {
    name: String(r.name ?? ""),
    kind: String(r.kind ?? ""),
    files,
    meta: (r.meta as Record<string, unknown>) ?? {},
    lint: normLint(r.lint),
  };
}

/** Starter files for a new item. The names follow the layer store's layout. */
export function templateFiles(kind: Kind, name: string): Record<string, string> {
  switch (kind) {
    case "agents":
      return {
        "agent.yaml": `title: ${name}\ndescription: What this agent type does.\nroute:\n  description: When to send work here.\n  not_for: What to keep away from it.\n`,
        "prompt.md": `You are the ${name} agent.\n`,
      };
    case "skills":
      return {
        "skill.yaml": `name: ${name}\ndescription: One line on when to use this skill.\nversion: "1"\ntypes: []\n`,
        "prompt.md": `# ${name}\n\nSteps the agent follows.\n`,
      };
    case "plays":
      return { "play.yaml": `name: ${name}\ndescription: What this play does.\n` };
    case "context":
      return { [name]: `# ${name}\n\nBackground the agents should know.\n` };
    case "data":
      return { [name]: "" };
    case "tools":
      return { "tools.yaml": "sources: []\nallow: []\n" };
  }
}

export function isMarkdownFile(path: string): boolean {
  return /\.(md|markdown|txt)$/i.test(path) || (!/\.[a-z0-9]{1,8}$/i.test(path));
}

export function isYamlFile(path: string): boolean {
  return /\.(ya?ml)$/i.test(path);
}

export function fileTag(path: string, kind: Kind): "Markdown" | "YAML" | "Text" {
  if (isYamlFile(path)) return "YAML";
  if (/\.(md|markdown)$/i.test(path) || kind === "context") return "Markdown";
  return "Text";
}

/** Names of lint findings that belong to one item. */
export function lintFor(lint: LintFinding[], kind: string, name: string): LintFinding[] {
  return lint.filter((l) => l.name === name && (!l.kind || l.kind === kind));
}

export function lintCounts(lint: LintFinding[]): { errors: number; warnings: number } {
  return {
    errors: lint.filter((l) => l.severity === "error").length,
    warnings: lint.filter((l) => l.severity !== "error").length,
  };
}

// ---- import ----

export interface ImportReport {
  created: string[];
  updated: string[];
  skipped: string[];
  invalid: { name: string; reason: string }[];
  warnings: string[];
  dry_run: boolean;
}

export function normImport(raw: unknown): ImportReport {
  const r = (raw ?? {}) as Record<string, unknown>;
  const strs = (v: unknown) => (Array.isArray(v) ? v.map(String) : []);
  const invalid = Array.isArray(r.invalid)
    ? (r.invalid as unknown[]).map((x) => {
        if (typeof x === "string") return { name: x, reason: "" };
        const o = (x ?? {}) as Record<string, unknown>;
        return { name: String(o.name ?? o.path ?? ""), reason: String(o.reason ?? o.message ?? o.error ?? "") };
      })
    : [];
  return {
    created: strs(r.created),
    updated: strs(r.updated),
    skipped: strs(r.skipped),
    invalid,
    warnings: strs(r.warnings),
    dry_run: r.dry_run === true,
  };
}

/** Files named SKILL.md (or any .md) in a pasted blob. A paste is one SKILL.md. */
export function pasteToFiles(text: string, pathHint = "pasted/SKILL.md"): Record<string, string> {
  return text.trim() ? { [pathHint]: text } : {};
}

// ---- resolved view ----

export interface ResolvedItem {
  name: string;
  source: string;
  enabled: boolean;
  detail: string;
}

export interface ResolvedTool {
  name: string;
  title: string;
  approval: string;
  source: string;
  alias: string[];
}

export interface RouteOption {
  type: string;
  description: string;
  not_for: string;
  source: string;
}

export interface Resolved {
  groups: Record<"agents" | "skills" | "context" | "data" | "plays", ResolvedItem[]>;
  tools: ResolvedTool[];
  routes: RouteOption[];
  layers: string[];
  warnings: string[];
  systemPrompt: string;
}

function lastOf(v: unknown): string {
  if (Array.isArray(v)) return v.length ? String(v[v.length - 1]) : "";
  return typeof v === "string" ? v : "";
}

function itemSource(r: Record<string, unknown>): string {
  return (
    lastOf(r.source_layer) ||
    lastOf(r.source) ||
    lastOf(r.layer) ||
    lastOf(r.sources) ||
    lastOf(r.prompt_sources)
  );
}

function describeItem(r: Record<string, unknown>): string {
  const cfg = (r.config as Record<string, unknown>) ?? {};
  const d = r.description ?? r.title ?? cfg.description ?? cfg.title ?? "";
  return typeof d === "string" ? d : "";
}

function normGroup(raw: unknown): ResolvedItem[] {
  if (!raw) return [];
  if (Array.isArray(raw)) {
    return raw.map((x) => {
      if (typeof x === "string") return { name: x, source: "", enabled: true, detail: "" };
      const r = (x ?? {}) as Record<string, unknown>;
      return { name: String(r.name ?? r.type ?? ""), source: itemSource(r), enabled: r.enabled !== false, detail: describeItem(r) };
    });
  }
  return Object.entries(raw as Record<string, unknown>).map(([name, v]) => {
    const r = (v && typeof v === "object" ? v : {}) as Record<string, unknown>;
    return { name, source: itemSource(r), enabled: r.enabled !== false, detail: describeItem(r) };
  });
}

function normToolRows(raw: unknown, fallbackSource = ""): ResolvedTool[] {
  if (!Array.isArray(raw)) return [];
  return raw.map((x) => {
    if (typeof x === "string") return { name: x, title: "", approval: "", source: fallbackSource, alias: [] };
    const r = (x ?? {}) as Record<string, unknown>;
    const alias = r.alias ?? r.aliases;
    return {
      name: String(r.name ?? ""),
      title: String(r.title ?? ""),
      approval: String(r.approval ?? ""),
      source: String(r.source_layer ?? r.layer ?? r.source ?? fallbackSource),
      alias: Array.isArray(alias) ? alias.map(String) : typeof alias === "string" && alias ? [alias] : [],
    };
  });
}

export function normResolved(raw: unknown): Resolved {
  const r = (raw ?? {}) as Record<string, unknown>;
  const t = r.tools;
  const toolsObj = t && !Array.isArray(t) && typeof t === "object" ? (t as Record<string, unknown>) : {};
  const rows =
    r.tool_set ?? r.toolset ?? r.resolved_tools ?? toolsObj.resolved ?? toolsObj.tools ?? toolsObj.set ?? (Array.isArray(t) ? t : undefined) ?? r.tools_for_type;
  let tools = normToolRows(rows);
  // A bare list of names from tools_for_type is still worth showing.
  if (!tools.length && Array.isArray(r.tools_for_type)) tools = normToolRows(r.tools_for_type);
  const routes = Array.isArray(r.routes)
    ? (r.routes as unknown[]).map((x) => {
        const o = (x ?? {}) as Record<string, unknown>;
        return {
          type: String(o.type ?? o.name ?? ""),
          description: String(o.description ?? ""),
          not_for: String(o.not_for ?? ""),
          source: String(o.source ?? o.source_layer ?? ""),
        };
      })
    : [];
  const agent = r.agent && typeof r.agent === "object" ? { [String((r.agent as Record<string, unknown>).type ?? "agent")]: r.agent } : undefined;
  const layers = Array.isArray(r.layers)
    ? (r.layers as unknown[]).map((l) => (typeof l === "string" ? l : String((l as { name?: string })?.name ?? ""))).filter(Boolean)
    : [];
  return {
    groups: {
      agents: normGroup(r.agents ?? agent),
      skills: normGroup(r.skills),
      context: normGroup(r.context),
      data: normGroup(r.data),
      plays: normGroup(r.plays),
    },
    tools,
    routes,
    layers,
    warnings: Array.isArray(r.warnings) ? (r.warnings as unknown[]).map(String) : [],
    systemPrompt: typeof r.system_prompt === "string" ? r.system_prompt : "",
  };
}

/** Fill title, approval and alias on resolved tools from catalog entries. */
export function enrichTools(tools: ResolvedTool[], catalog: CatalogEntry[]): ResolvedTool[] {
  const by = new Map(catalog.map((c) => [c.name, c]));
  return tools.map((t) => {
    const c = by.get(t.name);
    if (!c) return t;
    return {
      ...t,
      title: t.title || c.title,
      approval: t.approval || c.approval,
      source: t.source || c.source,
      alias: t.alias.length ? t.alias : c.alias,
    };
  });
}

// ---- tools ----

export const APPROVAL_CLASSES = ["read", "write", "always_approve"] as const;

export interface CatalogEntry {
  name: string;
  alias: string[];
  title: string;
  description: string;
  approval: string;
  source: string;
  layer: string;
  origin: string;
  endpoint: string;
  /** HTTP method and path split out of `endpoint`, empty when the tool has none. */
  method: string;
  path: string;
  kind: string;
  options: Record<string, unknown[]>;
  params: number;
  paramList: CatalogParam[];
  /** null when the server does not say; the catalog only carries it for some entries. */
  enabled: boolean | null;
}

export interface CatalogParam {
  name: string;
  type: string;
  required: boolean;
  location: string;
}

export function normCatalog(raw: unknown): CatalogEntry[] {
  const rawObj = (raw ?? {}) as Record<string, unknown>;
  const list = Array.isArray(raw)
    ? raw
    : Array.isArray(rawObj.entries)
      ? (rawObj.entries as unknown[])
      : Array.isArray(rawObj.tools)
        ? (rawObj.tools as unknown[])
        : [];
  return list.map((x) => {
    const r = (x ?? {}) as Record<string, unknown>;
    const alias = r.alias ?? r.aliases;
    let endpoint = "";
    let method = "";
    let path = "";
    let kind = String(r.kind ?? "");
    if (typeof r.endpoint === "string") {
      endpoint = r.endpoint;
      const m = /^([A-Za-z]+)\s+(\S.*)$/.exec(r.endpoint.trim());
      if (m) [method, path] = [m[1], m[2]];
      else path = r.endpoint.trim();
    } else {
      const e = (r.endpoint && typeof r.endpoint === "object" ? r.endpoint : r) as Record<string, unknown>;
      method = String(e.method ?? "");
      path = String(e.url ?? e.path ?? "");
      if (!kind && e !== r && e.kind) kind = String(e.kind);
      endpoint = [method, path].filter(Boolean).join(" ");
    }
    const params = Array.isArray(r.params) ? r.params : [];
    return {
      name: String(r.name ?? ""),
      alias: Array.isArray(alias) ? alias.map(String) : typeof alias === "string" && alias ? [alias] : [],
      title: String(r.title ?? ""),
      description: String(r.description ?? ""),
      approval: String(r.approval ?? ""),
      source: String(r.source ?? r.layer ?? ""),
      layer: String(r.layer ?? ""),
      origin: String(r.origin ?? ""),
      endpoint,
      method: method.toUpperCase(),
      path,
      kind,
      options: (r.options as Record<string, unknown[]>) ?? {},
      params: params.length,
      paramList: params.map((p) => {
        const o = (p && typeof p === "object" ? p : { name: p }) as Record<string, unknown>;
        return { name: String(o.name ?? ""), type: String(o.type ?? ""), required: o.required === true, location: String(o.location ?? "") };
      }),
      enabled: typeof r.enabled === "boolean" ? r.enabled : null,
    };
  });
}

export type ToolKind = "http" | "openapi" | "mcp";

export interface RegisterForm {
  name: string;
  kind: ToolKind;
  description: string;
  approval: string; // "" means let the server pick the default
  // http
  url: string;
  method: string;
  // openapi
  spec: string;
  baseUrl: string;
  operationIds: string;
  // mcp
  serverMode: "url" | "command";
  serverUrl: string;
  serverCommand: string;
  // auth: a secret reference name only, never a value
  secretRef: string;
  authHeader: string;
}

export const EMPTY_REGISTER: RegisterForm = {
  name: "",
  kind: "http",
  description: "",
  approval: "",
  url: "",
  method: "GET",
  spec: "",
  baseUrl: "",
  operationIds: "",
  serverMode: "url",
  serverUrl: "",
  serverCommand: "",
  secretRef: "",
  authHeader: "Authorization",
};

/** `provider:handle` style names. A long unbroken token with no colon is
 *  more likely a pasted key than a reference. */
export function looksLikeSecretValue(v: string): boolean {
  const s = v.trim();
  if (!s) return false;
  if (/^(sk|pk|ghp|gho|xox[a-z]?|AKIA|Bearer\s)/i.test(s)) return true;
  if (/\s/.test(s)) return true;
  return !s.includes(":") && s.length > 40;
}

export function validateRegister(f: RegisterForm): string[] {
  const errs: string[] = [];
  if (!validName(f.name)) errs.push("Name: lowercase letters, digits, dash or underscore.");
  if (!f.description.trim()) errs.push("Description is required.");
  if (f.kind === "http" && !/^https?:\/\//.test(f.url.trim())) errs.push("Endpoint must be an http(s) URL.");
  if (f.kind === "openapi") {
    if (!f.spec.trim()) errs.push("OpenAPI spec URL or path is required.");
    if (!f.operationIds.split(",").some((s) => s.trim())) errs.push("List at least one operation id.");
  }
  if (f.kind === "mcp") {
    if (f.serverMode === "url" && !/^https?:\/\//.test(f.serverUrl.trim())) errs.push("MCP server must be an http(s) URL.");
    if (f.serverMode === "command" && !f.serverCommand.trim()) errs.push("MCP server command is required.");
  }
  if (f.secretRef.trim() && looksLikeSecretValue(f.secretRef))
    errs.push("Auth takes a secret reference name like provider:handle, never the secret itself.");
  return errs;
}

/** The `decl` body for POST /tools/register. */
export function buildDecl(f: RegisterForm): Record<string, unknown> {
  const decl: Record<string, unknown> = {
    name: f.name.trim(),
    kind: f.kind,
    description: f.description.trim(),
  };
  if (f.approval) decl.approval = f.approval;
  if (f.kind === "http") {
    decl.endpoint = f.url.trim();
    decl.method = f.method;
  } else if (f.kind === "openapi") {
    const source: Record<string, unknown> = {
      spec: f.spec.trim(),
      operation_ids: f.operationIds.split(",").map((s) => s.trim()).filter(Boolean),
    };
    if (f.baseUrl.trim()) source.base_url = f.baseUrl.trim();
    decl.source = source;
  } else {
    decl.server =
      f.serverMode === "url"
        ? { url: f.serverUrl.trim() }
        : { command: f.serverCommand.trim().split(/\s+/).filter(Boolean) };
  }
  if (f.secretRef.trim()) {
    decl.auth = { secret_ref: f.secretRef.trim(), ...(f.authHeader.trim() && f.authHeader.trim() !== "Authorization" ? { header: f.authHeader.trim() } : {}) };
  }
  return decl;
}

export interface SyncDiff {
  added: string[];
  removed: string[];
  changed: string[];
}

function nameList(v: unknown): string[] {
  if (!Array.isArray(v)) return [];
  return v.map((x) => (typeof x === "string" ? x : String((x as { name?: string })?.name ?? JSON.stringify(x))));
}

export function normSync(raw: unknown): SyncDiff {
  const r = (raw ?? {}) as Record<string, unknown>;
  return { added: nameList(r.added), removed: nameList(r.removed), changed: nameList(r.changed) };
}

export interface ProbeResult {
  probed: string[];
  skipped: { name: string; reason: string }[];
}

export function normProbe(raw: unknown): ProbeResult {
  const r = (raw ?? {}) as Record<string, unknown>;
  const skipped = Array.isArray(r.skipped)
    ? (r.skipped as unknown[]).map((x) => {
        if (typeof x === "string") return { name: x, reason: "" };
        const o = (x ?? {}) as Record<string, unknown>;
        return { name: String(o.name ?? o.op ?? ""), reason: String(o.reason ?? "") };
      })
    : [];
  return { probed: nameList(r.probed), skipped };
}

// ---- HTTP ----

function qs(params: Record<string, string | undefined>): string {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) u.set(k, v);
  const s = u.toString();
  return s ? `?${s}` : "";
}

async function call<T = unknown>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { ...init, credentials: "same-origin" });
  } catch (e) {
    throw new SetupsError(`Cannot reach the server (${errorText(e)}).`);
  }
  if (res.status === 401) notifyUnauthorized();
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let body: unknown = undefined;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (!res.ok) {
    const b = (body ?? {}) as Record<string, unknown>;
    const reasons = Array.isArray(b.reasons) ? b.reasons.map(String) : Array.isArray(b.errors) ? b.errors.map(String) : [];
    const msg =
      res.status === 401
        ? "Sign in to manage setups."
        : res.status === 403
          ? "Writes need the admin role."
          : typeof b.error === "string"
            ? b.error
            : typeof b.message === "string"
              ? b.message
              : text.slice(0, 200) || `request failed (${res.status})`;
    throw new SetupsError(msg, res.status, reasons);
  }
  return body as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };
const post = <T,>(path: string, body: unknown) =>
  call<T>(path, { method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body) });

// ---- preview, ui config and tool usage ----

export interface PreviewSkill { name: string; score: number; full: boolean; source: string }
export interface PreviewTool {
  name: string;
  title: string | null;
  score: number;
  shown: boolean;
  pinned: boolean;
  approval: string | null;
  source: string | null;
}
export interface Preview {
  ranking: "embeddings" | "words";
  skillTopK: number | null;
  retrieval: { enabled: boolean; topK: number | null };
  always: { name: string; chars: number; source: string }[];
  onDemand: PreviewSkill[];
  moved: { name: string; reason: string }[];
  tools: PreviewTool[];
  promptChars: number;
  agentChars: number;
  skillsChars: number;
  contextChars: number;
  budgets: { prompt_chars: number; skills_total_chars: number; context_chars: number };
  warnings: string[];
}

const num = (v: unknown, d = 0): number => (typeof v === "number" && Number.isFinite(v) ? v : d);
const str = (v: unknown, d = ""): string => (typeof v === "string" ? v : d);
const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? v.filter((x): x is Record<string, unknown> => !!x && typeof x === "object") : [];

export function normPreview(raw: unknown): Preview {
  const r = (raw ?? {}) as Record<string, unknown>;
  const skills = (r.skills ?? {}) as Record<string, unknown>;
  const prompt = (r.prompt ?? {}) as Record<string, unknown>;
  const b = (prompt.budgets ?? {}) as Record<string, unknown>;
  const tr = (r.tool_retrieval ?? {}) as Record<string, unknown>;
  return {
    ranking: r.ranking === "embeddings" ? "embeddings" : "words",
    skillTopK: typeof r.skill_top_k === "number" ? r.skill_top_k : null,
    retrieval: { enabled: tr.enabled === true, topK: typeof tr.top_k === "number" ? tr.top_k : null },
    always: arr(skills.always).map((x) => ({ name: str(x.name), chars: num(x.chars), source: str(x.source) })),
    onDemand: arr(skills.on_demand).map((x) => ({ name: str(x.name), score: num(x.score), full: x.full === true, source: str(x.source) })),
    moved: arr(skills.moved).map((x) => ({ name: str(x.name), reason: str(x.reason) })),
    tools: arr(r.tools).map((x) => ({
      name: str(x.name),
      title: typeof x.title === "string" ? x.title : null,
      score: num(x.score),
      shown: x.shown === true,
      pinned: x.pinned === true,
      approval: typeof x.approval === "string" ? x.approval : null,
      source: typeof x.source === "string" ? x.source : null,
    })),
    promptChars: num(prompt.chars),
    agentChars: num(prompt.agent_chars),
    skillsChars: num(prompt.skills_chars),
    contextChars: num(prompt.context_chars),
    budgets: { prompt_chars: num(b.prompt_chars), skills_total_chars: num(b.skills_total_chars), context_chars: num(b.context_chars) },
    warnings: Array.isArray(r.warnings) ? r.warnings.map(String) : [],
  };
}

export const UI_TABS = ["agents", "skills", "companies", "projects", "tools", "preview", "merged"] as const;
export type UiTab = (typeof UI_TABS)[number];
export type UiLabelKey =
  | "agent_type" | "agent_types" | "company" | "companies" | "project" | "projects"
  | "global_skills" | "skills" | "context" | "data" | "tools" | "merged" | "preview"
  | "worker" | "workers";

export const DEFAULT_LABELS: Record<UiLabelKey, string> = {
  agent_type: "Agent type",
  agent_types: "Agent types",
  company: "Company",
  companies: "Companies",
  project: "Project",
  projects: "Projects",
  global_skills: "Global skills",
  skills: "Skills",
  context: "Context",
  data: "Data",
  tools: "Tools",
  merged: "Merged view",
  preview: "Try a task",
  worker: "Worker",
  workers: "Workers",
};

export interface UiConfig {
  title: string;
  intro: string;
  labels: Record<UiLabelKey, string>;
  tabs: UiTab[];
  defaultTab: UiTab;
}

export const DEFAULT_UI: UiConfig = {
  title: "Setups",
  intro:
    "An agent type is the kind of organization served. Workers are the agents every type gets. A company picks its type and adds its own context and skills, and each project adds more for one piece of work.",
  labels: DEFAULT_LABELS,
  tabs: [...UI_TABS],
  defaultTab: "agents",
};

/** Server `ui:` block over the defaults. Unknown keys and tabs are dropped. */
export function normUi(raw: unknown): UiConfig {
  const r = ((raw as Record<string, unknown> | undefined)?.ui ?? {}) as Record<string, unknown>;
  const labels = { ...DEFAULT_LABELS };
  const given = (r.labels ?? {}) as Record<string, unknown>;
  for (const k of Object.keys(DEFAULT_LABELS) as UiLabelKey[]) {
    if (typeof given[k] === "string" && given[k]) labels[k] = given[k] as string;
  }
  const tabs = Array.isArray(r.tabs) ? (r.tabs.filter((t) => (UI_TABS as readonly unknown[]).includes(t)) as UiTab[]) : [];
  const shown = tabs.length ? tabs : [...UI_TABS];
  const def = shown.includes(r.default_tab as UiTab) ? (r.default_tab as UiTab) : shown[0];
  return {
    title: typeof r.title === "string" && r.title ? r.title : DEFAULT_UI.title,
    intro: typeof r.intro === "string" && r.intro ? r.intro : DEFAULT_UI.intro,
    labels,
    tabs: shown,
    defaultTab: def,
  };
}

export interface ToolUsageRow { calls: number; failures: number; refused: number; seconds: number; runs: number }
export interface ToolUsage { runs: number; tools: Record<string, ToolUsageRow> }

export function normToolUsage(raw: unknown): ToolUsage {
  const r = (raw ?? {}) as Record<string, unknown>;
  const tools: Record<string, ToolUsageRow> = {};
  for (const [name, v] of Object.entries((r.tools ?? {}) as Record<string, unknown>)) {
    const x = (v ?? {}) as Record<string, unknown>;
    tools[name] = { calls: num(x.calls), failures: num(x.failures), refused: num(x.refused), seconds: num(x.seconds), runs: num(x.runs) };
  }
  return { runs: num(r.runs), tools };
}

export const setupsApi = {
  ui: async () => {
    try {
      return normUi(await call("/setups/ui"));
    } catch {
      return DEFAULT_UI; // older servers have no /setups/ui
    }
  },
  preview: async (q: { org?: string; project?: string; type: string; task: string; skillTopK?: number; toolTopK?: number }) =>
    normPreview(
      await call(
        `/setups/preview${qs({
          org: q.org,
          project: q.project,
          type: q.type,
          task: q.task,
          skill_top_k: q.skillTopK ? String(q.skillTopK) : undefined,
          tool_top_k: q.toolTopK ? String(q.toolTopK) : undefined,
        })}`,
      ),
    ),
  toolUsage: async () => {
    try {
      return normToolUsage(await call("/runs/tool-usage"));
    } catch {
      return { runs: 0, tools: {} } as ToolUsage; // no runs store or an older server
    }
  },
  scopes: async () => normScopes(await call("/setups/scopes")),
  createScope: (body: { kind: "pack" | "org" | "project"; name: string; org?: string }) => post("/setups/scopes", body),
  items: async (scope: string, kind: Kind) => normItems(await call(`/setups/items${qs({ scope, kind })}`)),
  item: async (scope: string, kind: Kind, name: string) => normItem(await call(`/setups/item${qs({ scope, kind, name })}`)),
  putItem: async (scope: string, kind: Kind, name: string, files: Record<string, string>) => {
    const r = (await call<{ lint?: unknown }>(`/setups/item${qs({ scope, kind, name })}`, {
      method: "PUT",
      headers: JSON_HEADERS,
      body: JSON.stringify({ files }),
    })) as { lint?: unknown } | undefined;
    return normLint(r?.lint);
  },
  deleteItem: (scope: string, kind: Kind, name: string) =>
    call<void>(`/setups/item${qs({ scope, kind, name })}`, { method: "DELETE" }),
  disable: (scope: string, kind: Kind, name: string, disabled: boolean) =>
    post("/setups/item/disable", { scope, kind, name, disabled }),
  /** companies/<org>/setup.yaml text. The server may not have the file yet. */
  orgSetup: async (scope: string): Promise<string> => {
    try {
      const d = normItem(await call(`/setups/item${qs({ scope, kind: "setup", name: "setup.yaml" })}`));
      return d.files["setup.yaml"] ?? Object.values(d.files)[0] ?? "";
    } catch (e) {
      if (e instanceof SetupsError && e.status === 404) return "";
      throw e;
    }
  },
  /** A scope's setup.yaml, parsed by the server. Empty when the scope has none. */
  setupMeta: async (scope: string): Promise<Record<string, unknown>> => {
    try {
      return normItem(await call(`/setups/item${qs({ scope, kind: "setup", name: "setup.yaml" })}`)).meta;
    } catch (e) {
      if (e instanceof SetupsError && e.status === 404) return {};
      throw e;
    }
  },
  putOrgSetup: async (scope: string, text: string) =>
    normLint(
      (
        (await call(`/setups/item${qs({ scope, kind: "setup", name: "setup.yaml" })}`, {
          method: "PUT",
          headers: JSON_HEADERS,
          body: JSON.stringify({ files: { "setup.yaml": text } }),
        })) as { lint?: unknown } | undefined
      )?.lint,
    ),
  lint: async (scope: string) => normLint(await call(`/setups/lint${qs({ scope })}`)),
  resolved: async (org?: string, project?: string, type?: string) =>
    normResolved(await call(`/setups/resolved${qs({ org, project, type })}`)),
  importItems: async (scope: string, format: "skill-md" | "fabri", files: Record<string, string>, dryRun: boolean) =>
    normImport(await post("/setups/import", { scope, format, files, dry_run: dryRun })),
  catalog: async (org?: string, project?: string, type?: string) =>
    normCatalog(await call(`/tools/catalog${qs({ org, project, type })}`)),
  register: (scope: string, decl: Record<string, unknown>) => post("/tools/register", { scope, decl }),
  unregister: (scope: string, name: string) => post("/tools/unregister", { scope, name }),
  enableTool: (scope: string, name: string, enabled: boolean) => post("/tools/enable", { scope, name, enabled }),
  sync: async (scope: string) => normSync(await post("/tools/sync", { scope })),
  probe: async (scope: string, ops?: string[]) =>
    normProbe(await post("/tools/probe", { scope, ...(ops && ops.length ? { ops } : {}) })),
};

// ---- company agent selection (companies/<org>/setup.yaml `agents: [...]`) ----

/** The `agents:` list in a setup.yaml, or null when the key is absent.
 *  Reads the inline form (`agents: [a, b]`) and the block form (`- a`). */
export function parseAgentsList(yaml: string): string[] | null {
  const lines = yaml.split("\n");
  const i = lines.findIndex((l) => /^agents\s*:/.test(l));
  if (i < 0) return null;
  const rest = lines[i].replace(/^agents\s*:/, "").replace(/\s+#.*$/, "").trim();
  const clean = (v: string) => v.trim().replace(/^["']|["']$/g, "");
  if (rest.startsWith("[")) {
    return rest.replace(/^\[|\]$/g, "").split(",").map(clean).filter(Boolean);
  }
  if (rest) return [clean(rest)];
  const out: string[] = [];
  for (let j = i + 1; j < lines.length; j++) {
    const m = lines[j].match(/^\s*-\s*(.+?)\s*$/);
    if (m) out.push(clean(m[1]));
    else if (lines[j].trim() === "" || /^\s*#/.test(lines[j])) continue;
    else break;
  }
  return out;
}

/** Replace (or add) the `agents:` list, leaving every other line as it was. */
export function setAgentsList(yaml: string, agents: string[]): string {
  const block = agents.length ? `agents:\n${agents.map((a) => `  - ${a}`).join("\n")}` : "agents: []";
  const lines = yaml.split("\n");
  const i = lines.findIndex((l) => /^agents\s*:/.test(l));
  if (i < 0) {
    const base = yaml.replace(/\s*$/, "");
    return (base ? base + "\n" : "") + block + "\n";
  }
  let end = i + 1;
  const inline = lines[i].replace(/^agents\s*:/, "").replace(/\s+#.*$/, "").trim();
  if (!inline) while (end < lines.length && (/^\s*-\s/.test(lines[end]) || lines[end].trim() === "")) end++;
  // Keep one blank line that trailed the block rather than eating it.
  while (end > i + 1 && lines[end - 1].trim() === "") end--;
  return [...lines.slice(0, i), ...block.split("\n"), ...lines.slice(end)].join("\n");
}

/** Agent types a skill targets: meta.types, else the `types:` list in skill.yaml. */
export function skillTypes(detail: ItemDetail): string[] {
  const m = detail.meta?.types;
  if (Array.isArray(m)) return m.map(String);
  const y = detail.files["skill.yaml"] ?? "";
  const lines = y.split("\n");
  const i = lines.findIndex((l) => /^types\s*:/.test(l));
  if (i < 0) return [];
  const rest = lines[i].replace(/^types\s*:/, "").trim();
  const clean = (v: string) => v.trim().replace(/^["']|["']$/g, "");
  if (rest.startsWith("[")) return rest.replace(/^\[|\]$/g, "").split(",").map(clean).filter(Boolean);
  const out: string[] = [];
  for (let j = i + 1; j < lines.length; j++) {
    const mm = lines[j].match(/^\s*-\s*(.+?)\s*$/);
    if (mm) out.push(clean(mm[1]));
    else break;
  }
  return out;
}

/** A skill template that targets one agent type. */
export function skillFilesForType(name: string, type: string): Record<string, string> {
  return {
    "skill.yaml": `name: ${name}\ndescription: One line on when to use this skill.\nversion: "1"\ntypes: [${type}]\n`,
    "prompt.md": `# ${name}\n\nSteps the ${type} agent follows.\n`,
  };
}

/** The org and project a catalog/resolved call should use for a scope. */
export function scopeContext(scope: Scope): { org?: string; project?: string } {
  return { org: scope.org, project: scope.project };
}

// ---- org types (packs) and global skills ----

export interface OrgType {
  name: string;
  title: string;
  description: string;
}

/** A pack's setup.yaml as an org type: `title` and `description`, falling back to the pack name. */
export function normOrgType(name: string, meta: Record<string, unknown>): OrgType {
  const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");
  return { name, title: str(meta.title) || name, description: str(meta.description) };
}

export interface GlobalSkill {
  name: string;
  what: string;
  workers: string[];
  code: string;
}

/** baseline setup.yaml `global_skills`: what every org type gets and the workers behind each. */
export function normGlobalSkills(meta: Record<string, unknown>): GlobalSkill[] {
  const list = Array.isArray(meta.global_skills) ? meta.global_skills : [];
  return list
    .map((x) => {
      const r = (x ?? {}) as Record<string, unknown>;
      return {
        name: String(r.name ?? "").trim(),
        what: String(r.what ?? "").trim(),
        workers: Array.isArray(r.workers) ? r.workers.map(String) : [],
        code: typeof r.code === "string" ? r.code : "",
      };
    })
    .filter((s) => s.name);
}

/** Workers no global skill names (chat and none route and reply). */
export function unlistedWorkers(skills: GlobalSkill[], workers: string[]): string[] {
  const named = new Set(skills.flatMap((s) => s.workers));
  return workers.filter((w) => !named.has(w));
}

/** The companies that picked each pack, from the scope tree. */
export function packCompanies(tree: ScopeTree): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const o of tree.orgs) for (const p of o.packs) (out[p] ??= []).push(o.name);
  return out;
}
