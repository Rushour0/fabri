import { describe, it, expect, vi } from "vitest";
import { connectUrl, expiryLabel, normAttachReport, normConnections, normConnectors } from "./connections";

describe("normConnections", () => {
  it("drops rows without a provider or handle and coerces mine", () => {
    const rows = normConnections({
      connections: [
        { provider: "slack", handle: "main", scope_kind: "org", mine: "yes" },
        { provider: "slack" },
        { handle: "x" },
        { provider: "linear", handle: "u1", scope_kind: "user", mine: true, expires_at: 100 },
        "junk",
      ],
    });
    expect(rows.map((r) => r.provider)).toEqual(["slack", "linear"]);
    expect(rows[0].mine).toBe(false);
    expect(rows[1].mine).toBe(true);
    expect(rows[1].scope_kind).toBe("user");
    expect(rows[1].expires_at).toBe(100);
  });
  it("returns nothing for odd input", () => {
    expect(normConnections(null)).toEqual([]);
    expect(normConnections({})).toEqual([]);
  });
  it("never carries token keys through", () => {
    const [row] = normConnections([{ provider: "a", handle: "b", access_token: "secret" }]);
    expect(JSON.stringify(row)).not.toContain("secret");
  });
});

describe("normConnectors", () => {
  it("defaults the booleans and scope", () => {
    const [c] = normConnectors([{ name: "slack" }]);
    expect(c).toMatchObject({
      name: "slack",
      title: "slack",
      auth_kind: "token",
      scope_default: "org",
      oauth: false,
      oauth_configured: false,
      oauth_loopback: false,
      curated: false,
      experimental: false,
    });
  });
  it("keeps set fields and skips nameless entries", () => {
    const list = normConnectors([{ title: "x" }, { name: "gmail", scope_default: "user", oauth: true, oauth_configured: true, experimental: true }]);
    expect(list).toHaveLength(1);
    expect(list[0].scope_default).toBe("user");
    expect(list[0].oauth_configured).toBe(true);
    expect(list[0].experimental).toBe(true);
  });
});

describe("connectUrl", () => {
  it("encodes the provider and carries the scope", () => {
    expect(connectUrl("slack", "org")).toBe("/connect/slack?scope=org");
    expect(connectUrl("a/b c", "user")).toBe("/connect/a%2Fb%20c?scope=user");
  });
});

describe("expiryLabel", () => {
  const now = 1_000_000;
  it("renders no expiry, days, hours, minutes and expired", () => {
    expect(expiryLabel(null, now)).toBe("no expiry");
    expect(expiryLabel(now + 3 * 86400 + 5, now)).toBe("expires in 3 d");
    expect(expiryLabel(now + 7200, now)).toBe("expires in 2 h");
    expect(expiryLabel(now + 600, now)).toBe("expires in 10 min");
    expect(expiryLabel(now - 1, now)).toBe("expired");
    expect(expiryLabel(now, now)).toBe("expired");
  });
});

describe("normAttachReport", () => {
  it("reads the tool list and tolerates missing fields", () => {
    const r = normAttachReport({ name: "slack", tools: [{ name: "post", approval: "write", kind: "mcp" }, {}] });
    expect(r.tools).toEqual([{ name: "post", approval: "write", kind: "mcp" }]);
    expect(r.tool_count).toBe(1);
    expect(r.errors).toEqual([]);
  });
});

describe("error text", () => {
  it("joins the error and its reasons", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ error: "tool source not added", reasons: ["policy refuses kind mcp"] }), { status: 409 }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const mod = await import("./connections");
    await expect(mod.connectionsApi.list()).rejects.toThrow("tool source not added: policy refuses kind mcp");
    vi.unstubAllGlobals();
  });
});
