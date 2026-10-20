// Model spend from pba's ledger (GET /v1/spend, /v1/spend/summary), read
// through the dev server's /pba/v1/spend proxy, which adds the service token
// and company. Pure shaping helpers live here so they test without a DOM.

import { notifyUnauthorized } from "./api";

export const SPEND_BASE = "/pba/v1/spend";
export const SPEND_PAGE = 25;
export const DAYS_SHOWN = 30;

export interface SpendRow {
  id: number;
  ts: string;
  company: string | null;
  project: string | null;
  actor: string | null;
  agent_type: string | null;
  purpose: string;
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cost_usd: number;
  ms: number;
  thread_id: string | null;
  task_id: string | null;
  run_id: string | null;
  ok: boolean;
  estimated: boolean;
  backfill: boolean;
  label: string | null;
}

export interface SpendGroup {
  key: string;
  calls: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
}

export interface SpendTotal {
  calls: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  estimated_usd: number;
  backfill_usd: number;
  failed: number;
}

export interface SpendQuery {
  group: string;
  total: SpendTotal;
  groups: SpendGroup[];
  rows: SpendRow[];
  rows_total: number;
  offset: number;
  limit: number;
  purposes: string[];
}

export interface SpendWindow {
  calls: number;
  cost_usd: number;
}

export interface SpendSummary {
  today: SpendWindow;
  "7d": SpendWindow;
  all: SpendWindow;
  by_purpose: { key: string; calls: number; cost_usd: number }[];
}

export class SpendError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin" });
  } catch (e) {
    throw new SpendError(`Cannot reach pba (${e instanceof Error ? e.message : String(e)}).`);
  }
  if (res.status === 401) notifyUnauthorized();
  const text = await res.text();
  let body: unknown;
  try {
    body = text ? JSON.parse(text) : undefined;
  } catch {
    body = undefined;
  }
  if (!res.ok) {
    const detail = (body as { detail?: unknown } | undefined)?.detail;
    const why = typeof detail === "string" ? detail : res.statusText || "request failed";
    const hint = res.status === 502 || res.status === 504 ? " Is pba running on 127.0.0.1:8790?" : "";
    throw new SpendError(`pba said ${res.status}: ${why}.${hint}`, res.status);
  }
  return body as T;
}

export interface SpendParams {
  group?: "purpose" | "agent_type" | "model" | "day" | "task";
  since?: string;
  until?: string;
  purpose?: string;
  limit?: number;
  offset?: number;
}

export function spendUrl(p: SpendParams = {}): string {
  const q = new URLSearchParams();
  if (p.group) q.set("group", p.group);
  if (p.since) q.set("since", p.since);
  if (p.until) q.set("until", p.until);
  if (p.purpose) q.set("purpose", p.purpose);
  if (p.limit !== undefined) q.set("limit", String(p.limit));
  if (p.offset) q.set("offset", String(p.offset));
  const s = q.toString();
  return s ? `${SPEND_BASE}?${s}` : SPEND_BASE;
}

export const spendApi = {
  query: (p: SpendParams) => getJson<SpendQuery>(spendUrl(p)),
  summary: () => getJson<SpendSummary>(`${SPEND_BASE}/summary`),
};

// ---- formatting ----

/** Dollars: 4 decimals under $1 so Jev-size costs stay visible, 2 above. */
export function formatUsd(v: number): string {
  const n = Number.isFinite(v) ? v : 0;
  const sign = n < 0 ? "-" : "";
  const a = Math.abs(n);
  if (a < 1) return `${sign}$${a.toFixed(4)}`;
  return `${sign}$${a.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** 1234 -> "1,234"; 1234567 -> "1.2M". */
export function formatTokens(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  return Math.round(n).toLocaleString("en-US");
}

/** "Oct 20, 14:05" in the viewer's zone; the raw value when it does not parse. */
export function formatTime(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts || "-";
  return d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

/** chat_turn -> "chat turn". */
export function purposeLabel(p: string): string {
  return p.replace(/_/g, " ");
}

// ---- shaping ----

export interface DayBar {
  day: string; // YYYY-MM-DD (UTC, as the ledger groups)
  cost_usd: number;
  calls: number;
  /** 0..1 of the tallest day */
  height: number;
}

function utcDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** First day of the window, as the `since` the API takes. */
export function windowStart(now: Date, days = DAYS_SHOWN): string {
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  start.setUTCDate(start.getUTCDate() - (days - 1));
  return utcDay(start);
}

/** One bar per day for the last `days` days, ending today; days with no calls are 0. */
export function dayBars(groups: SpendGroup[], now: Date, days = DAYS_SHOWN): DayBar[] {
  const byDay = new Map(groups.map((g) => [g.key, g]));
  const start = new Date(`${windowStart(now, days)}T00:00:00Z`);
  const out: DayBar[] = [];
  for (let i = 0; i < days; i++) {
    const d = new Date(start);
    d.setUTCDate(start.getUTCDate() + i);
    const key = utcDay(d);
    const g = byDay.get(key);
    out.push({ day: key, cost_usd: g?.cost_usd ?? 0, calls: g?.calls ?? 0, height: 0 });
  }
  const max = Math.max(0, ...out.map((b) => b.cost_usd));
  return out.map((b) => ({ ...b, height: max > 0 ? b.cost_usd / max : 0 }));
}

export interface Share {
  key: string;
  label: string;
  cost_usd: number;
  calls: number;
  /** 0..1 of the total */
  share: number;
}

/** Groups as shares of their sum, biggest first; past `top` the rest fold into "other". */
export function shares(groups: SpendGroup[] | SpendSummary["by_purpose"], top = 8, label: (k: string) => string = (k) => k || "none"): Share[] {
  const sorted = [...groups].sort((a, b) => b.cost_usd - a.cost_usd || a.key.localeCompare(b.key));
  const total = sorted.reduce((s, g) => s + g.cost_usd, 0);
  const head = sorted.slice(0, top);
  const tail = sorted.slice(top);
  const rows = head.map((g) => ({ key: g.key, label: label(g.key), cost_usd: g.cost_usd, calls: g.calls }));
  if (tail.length) {
    rows.push({
      key: "__rest",
      label: `${tail.length} more`,
      cost_usd: tail.reduce((s, g) => s + g.cost_usd, 0),
      calls: tail.reduce((s, g) => s + g.calls, 0),
    });
  }
  return rows.map((r) => ({ ...r, share: total > 0 ? r.cost_usd / total : 0 }));
}

/** "12%", or "<1%" for a sliver that is not zero. */
export function percent(share: number): string {
  if (share <= 0) return "0%";
  if (share < 0.01) return "<1%";
  return `${Math.round(share * 100)}%`;
}

export interface Paging {
  page: number; // zero-based
  pages: number;
  from: number;
  to: number;
  total: number;
  offset: number;
}

/** Server-side pages: which offset to ask for, and the "26-50 of 812" numbers. */
export function paging(total: number, page: number, size = SPEND_PAGE): Paging {
  const pages = Math.max(1, Math.ceil(Math.max(0, total) / size));
  const p = Math.min(Math.max(0, Math.floor(page) || 0), pages - 1);
  const offset = p * size;
  const to = Math.min(total, offset + size);
  return { page: p, pages, from: total ? offset + 1 : 0, to, total, offset };
}

/** Where a call happened: the task wins, then the thread, then the run. */
export function callWhere(r: SpendRow): { kind: "task" | "thread" | "run" | "none"; id: string } {
  if (r.task_id) return { kind: "task", id: r.task_id };
  if (r.thread_id) return { kind: "thread", id: r.thread_id };
  if (r.run_id) return { kind: "run", id: r.run_id };
  return { kind: "none", id: "" };
}

/** A short id for a table cell: uuids keep their first 8 characters. */
export function shortId(id: string): string {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(id) ? id.slice(0, 8) : id.length > 24 ? `${id.slice(0, 22)}…` : id;
}
