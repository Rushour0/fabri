import { describe, it, expect } from "vitest";
import {
  buildDecl,
  EMPTY_REGISTER,
  enrichTools,
  looksLikeSecretValue,
  normCatalog,
  normLint,
  normResolved,
  normScopes,
  normSync,
  parseAgentsList,
  projectScope,
  setAgentsList,
  skillTypes,
  validateRegister,
  validName,
  normItem,
} from "./setups";

describe("scopes", () => {
  it("builds the scope strings the API takes", () => {
    expect(projectScope("acme", "web").id).toBe("org:acme/project:web");
  });
  it("normalizes the scope tree", () => {
    const t = normScopes({ baseline: {}, packs: ["p"], orgs: [{ name: "acme", packs: ["p"], projects: ["web"] }] });
    expect(t.orgs[0].projects).toEqual(["web"]);
  });
  it("validates names", () => {
    expect(validName("acme-1")).toBe(true);
    expect(validName("../x")).toBe(false);
    expect(validName("Acme")).toBe(false);
  });
});

describe("company agents list in setup.yaml", () => {
  it("returns null when the key is absent", () => {
    expect(parseAgentsList("packs: [a]\n")).toBeNull();
  });
  it("reads inline and block forms", () => {
    expect(parseAgentsList("agents: [chat, canvas]\n")).toEqual(["chat", "canvas"]);
    expect(parseAgentsList("name: x\nagents:\n  - chat\n  - 'tasks'\npacks: [a]\n")).toEqual(["chat", "tasks"]);
  });
  it("replaces the block and keeps other keys", () => {
    const out = setAgentsList("packs: [a]\nagents:\n  - chat\n  - canvas\nbudget: 3\n", ["tasks"]);
    expect(out).toBe("packs: [a]\nagents:\n  - tasks\nbudget: 3\n");
    expect(parseAgentsList(out)).toEqual(["tasks"]);
  });
  it("appends when the key is missing, writes [] for none", () => {
    expect(setAgentsList("packs: [a]\n", ["chat"])).toBe("packs: [a]\nagents:\n  - chat\n");
    expect(setAgentsList("", [])).toBe("agents: []\n");
    expect(setAgentsList("agents: [a, b]\nx: 1\n", [])).toBe("agents: []\nx: 1\n");
  });
});

describe("skill types", () => {
  const mk = (files: Record<string, string>, meta = {}) => normItem({ name: "s", kind: "skills", files, meta });
  it("prefers meta", () => expect(skillTypes(mk({}, { types: ["chat"] }))).toEqual(["chat"]));
  it("parses skill.yaml", () => {
    expect(skillTypes(mk({ "skill.yaml": "name: s\ntypes: [chat, canvas]\n" }))).toEqual(["chat", "canvas"]);
    expect(skillTypes(mk({ "skill.yaml": "name: s\ntypes:\n  - tasks\n" }))).toEqual(["tasks"]);
    expect(skillTypes(mk({ "skill.yaml": "name: s\ntypes: []\n" }))).toEqual([]);
  });
});

describe("normalizers", () => {
  it("maps lint findings from either field naming", () => {
    expect(normLint([{ severity: "error", kind: "skills", name: "a", message: "m" }])[0].severity).toBe("error");
    expect(normLint([{ level: "warning", item: "a", message: "m" }])[0].name).toBe("a");
  });
  it("normalizes resolved items, tools and routes", () => {
    const r = normResolved({
      layers: [{ name: "baseline" }, { name: "org:acme" }],
      agents: { chat: { sources: ["baseline", "org:acme"], enabled: true, config: { title: "Chat" } } },
      skills: { s1: { source_layer: "baseline" } },
      routes: [{ type: "chat", description: "d", not_for: "n", source: "baseline" }],
      tools: { resolved: [{ name: "t", approval: "read", layer: "baseline" }] },
    });
    expect(r.groups.agents[0]).toMatchObject({ name: "chat", source: "org:acme", detail: "Chat" });
    expect(r.groups.skills[0].source).toBe("baseline");
    expect(r.tools[0].approval).toBe("read");
    expect(r.routes[0].type).toBe("chat");
    expect(r.layers).toEqual(["baseline", "org:acme"]);
  });
  it("enriches resolved tools from the catalog", () => {
    const tools = enrichTools(
      [{ name: "t", title: "", approval: "", source: "", alias: [] }],
      normCatalog([{ name: "t", title: "Look up", approval: "write", source: "crm", alias: "lk", method: "GET", path: "/a" }]),
    );
    expect(tools[0]).toMatchObject({ title: "Look up", approval: "write", alias: ["lk"] });
  });
  it("builds a catalog endpoint from method and path or an object", () => {
    expect(normCatalog([{ name: "a", method: "GET", path: "/x" }])[0].endpoint).toBe("GET /x");
    expect(normCatalog([{ name: "a", endpoint: { method: "POST", url: "https://h/x" } }])[0].endpoint).toBe("POST https://h/x");
  });
  it("normalizes sync diffs", () => {
    expect(normSync({ added: ["a"], removed: [], changed: [{ name: "c" }] })).toEqual({ added: ["a"], removed: [], changed: ["c"] });
  });
});

describe("tool registration", () => {
  const ok = { ...EMPTY_REGISTER, name: "lookup", description: "d", url: "https://api.x/y" };
  it("accepts a valid http tool and builds the decl", () => {
    expect(validateRegister(ok)).toEqual([]);
    expect(buildDecl(ok)).toEqual({ name: "lookup", kind: "http", description: "d", endpoint: "https://api.x/y", method: "GET" });
  });
  it("sends auth as a secret reference only", () => {
    const d = buildDecl({ ...ok, secretRef: "acme:key" });
    expect(d.auth).toEqual({ secret_ref: "acme:key" });
  });
  it("rejects pasted secrets", () => {
    expect(looksLikeSecretValue("sk-abc123")).toBe(true);
    expect(looksLikeSecretValue("Bearer abc")).toBe(true);
    expect(looksLikeSecretValue("acme:key")).toBe(false);
    expect(validateRegister({ ...ok, secretRef: "sk-live-123" })).not.toEqual([]);
  });
  it("requires fields per kind", () => {
    expect(validateRegister({ ...EMPTY_REGISTER, kind: "openapi", name: "a", description: "d" }).length).toBe(2);
    const d = buildDecl({ ...EMPTY_REGISTER, kind: "openapi", name: "a", description: "d", spec: "https://s", operationIds: "x, y" });
    expect(d.source).toEqual({ spec: "https://s", operation_ids: ["x", "y"] });
    expect(buildDecl({ ...EMPTY_REGISTER, kind: "mcp", name: "m", description: "d", serverMode: "command", serverCommand: "npx srv -y" }).server).toEqual({ command: ["npx", "srv", "-y"] });
  });
});
