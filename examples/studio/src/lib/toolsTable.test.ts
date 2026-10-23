import { describe, it, expect } from "vitest";
import { normCatalog } from "./setups";
import { facet, filterRows, NO_FILTER, paginate, rangeLabel, toToolRows } from "./toolsTable";

const METHODS = ["GET", "POST", "PATCH"];
const APPR = ["read", "write", "write"];

// 422 operations, shaped like the live /tools/catalog payload.
const raw = Array.from({ length: 422 }, (_, i) => ({
  name: `op_${String(i).padStart(3, "0")}`,
  title: i === 7 ? "List add-ons" : `Operation ${i}`,
  alias: i === 9 ? ["admin_add_ons_list"] : [],
  approval: APPR[i % 3],
  source: "nycbbl",
  layer: "baseline",
  origin: "declared",
  endpoint: { method: METHODS[i % 3], path: `/open-api/admin/thing-${i}/`, kind: "openapi" },
  params: [{ name: "limit", type: "integer", required: false, location: "query" }],
}));
const rows = toToolRows(normCatalog(raw));

describe("tool rows", () => {
  it("splits method and path and keeps params", () => {
    expect(rows[1]).toMatchObject({ name: "op_001", method: "POST", path: "/open-api/admin/thing-1/", approval: "write", layer: "baseline" });
    expect(rows[0].params[0]).toEqual({ name: "limit", type: "integer", required: false, location: "query" });
  });
  it("splits a string endpoint too", () => {
    const [e] = normCatalog([{ name: "a", endpoint: "delete /x/{id}" }]);
    expect([e.method, e.path]).toEqual(["DELETE", "/x/{id}"]);
  });
  it("takes enabled from the entry, then the registered list, else on", () => {
    const cat = normCatalog([{ name: "a" }, { name: "b", origin: "registered" }, { name: "c", enabled: false }]);
    const r = toToolRows(cat, [{ name: "b", kind: "tools", source_layer: "org:x", size: null, updated_at: null, enabled: false }]);
    expect(r.map((x) => x.enabled)).toEqual([true, false, false]);
  });
});

describe("filter", () => {
  it("returns everything with no filter", () => {
    expect(filterRows(rows, NO_FILTER)).toHaveLength(422);
  });
  it("searches name, title, alias and path, case-insensitive", () => {
    expect(filterRows(rows, { ...NO_FILTER, q: "OP_010" }).map((r) => r.name)).toEqual(["op_010"]);
    expect(filterRows(rows, { ...NO_FILTER, q: "add-ons" }).map((r) => r.name)).toEqual(["op_007"]);
    expect(filterRows(rows, { ...NO_FILTER, q: "admin_add_ons" }).map((r) => r.name)).toEqual(["op_009"]);
    expect(filterRows(rows, { ...NO_FILTER, q: "thing-421/" }).map((r) => r.name)).toEqual(["op_421"]);
  });
  it("needs every word to match", () => {
    expect(filterRows(rows, { ...NO_FILTER, q: "list add" }).map((r) => r.name)).toEqual(["op_007", "op_009"]);
    expect(filterRows(rows, { ...NO_FILTER, q: "list nothing" })).toHaveLength(0);
  });
  it("filters by method and approval, combined with search", () => {
    expect(filterRows(rows, { ...NO_FILTER, method: "GET" })).toHaveLength(141);
    expect(filterRows(rows, { ...NO_FILTER, approval: "write" })).toHaveLength(281);
    expect(filterRows(rows, { q: "thing-1", method: "POST", approval: "write" }).every((r) => r.method === "POST" && r.path.includes("thing-1"))).toBe(true);
  });
  it("counts facets in a stable order", () => {
    expect(facet(rows, "method")).toEqual([
      { value: "GET", count: 141 },
      { value: "POST", count: 141 },
      { value: "PATCH", count: 140 },
    ]);
    expect(facet(rows, "approval").map((f) => f.value)).toEqual(["read", "write"]);
  });
});

describe("paginate", () => {
  it("slices pages and labels the range", () => {
    const p = paginate(rows, 0, 25);
    expect(p.rows).toHaveLength(25);
    expect(p.pages).toBe(17);
    expect(rangeLabel(p)).toBe("1-25 of 422");
    expect(rangeLabel(paginate(rows, 1, 50))).toBe("51-100 of 422");
  });
  it("shows a short last page", () => {
    const p = paginate(rows, 16, 25);
    expect(p.rows).toHaveLength(22);
    expect(rangeLabel(p)).toBe("401-422 of 422");
  });
  it("clamps out-of-range pages", () => {
    expect(paginate(rows, 99, 25).page).toBe(16);
    expect(paginate(rows, -3, 25).page).toBe(0);
  });
  it("handles an empty result", () => {
    const p = paginate([], 3, 25);
    expect(p).toMatchObject({ page: 0, pages: 1, from: 0, to: 0, total: 0 });
    expect(rangeLabel(p)).toBe("0 of 0");
  });
});
