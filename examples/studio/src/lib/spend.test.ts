import { describe, it, expect } from "vitest";
import { callWhere, dayBars, formatTokens, formatUsd, paging, percent, shares, shortId, spendUrl, windowStart, type SpendRow } from "./spend";

const g = (key: string, cost_usd: number, calls = 1) => ({ key, cost_usd, calls, input_tokens: 0, output_tokens: 0 });

describe("spend formatting", () => {
  it("shows 4 decimals under a dollar and 2 above", () => {
    expect(formatUsd(0)).toBe("$0.0000");
    expect(formatUsd(0.00012)).toBe("$0.0001");
    expect(formatUsd(0.98765)).toBe("$0.9877");
    expect(formatUsd(1)).toBe("$1.00");
    expect(formatUsd(1234.5)).toBe("$1,234.50");
    expect(formatUsd(Number.NaN)).toBe("$0.0000");
  });
  it("shortens token counts and percents", () => {
    expect(formatTokens(1234)).toBe("1,234");
    expect(formatTokens(2_500_000)).toBe("2.5M");
    expect(formatTokens(-3)).toBe("0");
    expect(percent(0)).toBe("0%");
    expect(percent(0.004)).toBe("<1%");
    expect(percent(0.456)).toBe("46%");
  });
});

describe("spend shaping", () => {
  const now = new Date("2026-10-20T15:00:00Z");

  it("fills every day of the window and scales to the tallest", () => {
    const bars = dayBars([g("2026-10-18", 0.5, 3), g("2026-10-20", 1, 2), g("2026-09-01", 9)], now, 5);
    expect(bars.map((b) => b.day)).toEqual(["2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]);
    expect(bars.map((b) => b.height)).toEqual([0, 0, 0.5, 0, 1]);
    expect(bars[2].calls).toBe(3);
    expect(windowStart(now, 5)).toBe("2026-10-16");
  });
  it("keeps an empty window flat", () => {
    expect(dayBars([], now, 3).every((b) => b.height === 0 && b.cost_usd === 0)).toBe(true);
  });
  it("turns groups into shares that add up, folding the tail", () => {
    const out = shares([g("a", 1), g("b", 3), g("c", 0.5), g("d", 0.5)], 2);
    expect(out.map((s) => s.key)).toEqual(["b", "a", "__rest"]);
    expect(out[2].label).toBe("2 more");
    expect(out[2].cost_usd).toBe(1);
    expect(out.reduce((s, x) => s + x.share, 0)).toBeCloseTo(1);
    expect(shares([g("x", 0)])[0].share).toBe(0);
  });
  it("pages on the server with clamped offsets", () => {
    expect(paging(0, 0)).toMatchObject({ pages: 1, from: 0, to: 0, offset: 0 });
    expect(paging(60, 1, 25)).toMatchObject({ page: 1, pages: 3, from: 26, to: 50, offset: 25 });
    expect(paging(60, 9, 25)).toMatchObject({ page: 2, from: 51, to: 60, offset: 50 });
  });
  it("builds the query string the API takes", () => {
    expect(spendUrl()).toBe("/pba/v1/spend");
    expect(spendUrl({ group: "day", since: "2026-10-01", purpose: "chat_turn", limit: 1, offset: 0 })).toBe(
      "/pba/v1/spend?group=day&since=2026-10-01&purpose=chat_turn&limit=1",
    );
  });
  it("says where a call ran, task first", () => {
    const row = { task_id: null, thread_id: "t1", run_id: "r1" } as SpendRow;
    expect(callWhere(row)).toEqual({ kind: "thread", id: "t1" });
    expect(callWhere({ ...row, task_id: "k" })).toEqual({ kind: "task", id: "k" });
    expect(callWhere({ ...row, thread_id: null })).toEqual({ kind: "run", id: "r1" });
    expect(shortId("0c4c62cf-7873-4acc-bd32-36d5cc4ce804")).toBe("0c4c62cf");
  });
});
