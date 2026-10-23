import type { CatalogEntry, CatalogParam, SetupItem } from "./setups";

// Pure helpers behind the Tools table: one row per catalog entry, then
// search, facet filters and pagination. Kept out of the component so they
// can be tested without a DOM.

export interface ToolRow {
  name: string;
  title: string;
  description: string;
  alias: string[];
  method: string;
  path: string;
  approval: string;
  enabled: boolean;
  layer: string;
  origin: string;
  source: string;
  params: CatalogParam[];
}

export const PAGE_SIZES = [25, 50] as const;

/** Merge the catalog with the registered list. The catalog only lists tools
 *  that resolve in the scope, so an entry with no other signal counts as on. */
export function toToolRows(catalog: CatalogEntry[], registered: SetupItem[] = []): ToolRow[] {
  const reg = new Map(registered.map((t) => [t.name, t]));
  return catalog.map((e) => {
    const r = reg.get(e.name);
    const enabled = e.enabled ?? r?.enabled ?? true;
    return {
      name: e.name,
      title: e.title,
      description: e.description,
      alias: e.alias,
      method: e.method,
      path: e.path,
      approval: e.approval,
      enabled,
      layer: e.layer || e.source,
      origin: e.origin,
      source: e.source,
      params: e.paramList,
    };
  });
}

export interface ToolFilter {
  q: string;
  method: string; // "" means any
  approval: string; // "" means any
}

export const NO_FILTER: ToolFilter = { q: "", method: "", approval: "" };

/** Search matches name, title, alias and path; every word must match somewhere. */
export function filterRows(rows: ToolRow[], f: ToolFilter): ToolRow[] {
  const words = f.q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  return rows.filter((r) => {
    if (f.method && r.method !== f.method) return false;
    if (f.approval && r.approval !== f.approval) return false;
    if (!words.length) return true;
    const hay = `${r.name} ${r.title} ${r.alias.join(" ")} ${r.path}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** Distinct non-empty values with counts, in first-seen order for methods. */
export function facet(rows: ToolRow[], key: "method" | "approval"): { value: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const r of rows) {
    const v = r[key];
    if (v) counts.set(v, (counts.get(v) ?? 0) + 1);
  }
  const order = key === "method" ? ["GET", "POST", "PUT", "PATCH", "DELETE"] : ["read", "write", "always_approve"];
  const rank = (v: string) => {
    const i = order.indexOf(v);
    return i === -1 ? order.length : i;
  };
  return [...counts.entries()]
    .map(([value, count]) => ({ value, count }))
    .sort((a, b) => rank(a.value) - rank(b.value) || a.value.localeCompare(b.value));
}

export interface Page<T> {
  rows: T[];
  /** zero-based, clamped into range */
  page: number;
  pages: number;
  /** one-based first and last row shown; both 0 when empty */
  from: number;
  to: number;
  total: number;
}

export function paginate<T>(rows: T[], page: number, size: number): Page<T> {
  const total = rows.length;
  const safeSize = size > 0 ? Math.floor(size) : 25;
  const pages = Math.max(1, Math.ceil(total / safeSize));
  const p = Math.min(Math.max(0, Math.floor(page) || 0), pages - 1);
  const start = p * safeSize;
  const slice = rows.slice(start, start + safeSize);
  return { rows: slice, page: p, pages, from: slice.length ? start + 1 : 0, to: start + slice.length, total };
}

/** "1-25 of 422", or "0 of 0" when nothing matches. */
export function rangeLabel(p: Page<unknown>): string {
  return p.total === 0 ? "0 of 0" : `${p.from}-${p.to} of ${p.total}`;
}
