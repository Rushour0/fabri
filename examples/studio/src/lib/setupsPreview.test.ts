import { describe, expect, it } from "vitest";
import { DEFAULT_UI, normPreview, normToolUsage, normUi } from "./setups";

describe("ui config", () => {
  it("falls back to defaults", () => {
    expect(normUi(undefined)).toEqual(DEFAULT_UI);
    expect(normUi({ ui: {} }).tabs).toContain("preview");
  });
  it("applies labels, tabs and the default tab, dropping unknowns", () => {
    const ui = normUi({
      ui: {
        title: "Support setups",
        labels: { agent_types: "Roles", bogus: "x", company: "" },
        tabs: ["companies", "nope", "preview"],
        default_tab: "preview",
      },
    });
    expect(ui.title).toBe("Support setups");
    expect(ui.labels.agent_types).toBe("Roles");
    expect(ui.labels.company).toBe("Company");
    expect(ui.tabs).toEqual(["companies", "preview"]);
    expect(ui.defaultTab).toBe("preview");
  });
  it("uses the first shown tab when default_tab is hidden", () => {
    expect(normUi({ ui: { tabs: ["tools", "merged"], default_tab: "agents" } }).defaultTab).toBe("tools");
  });
});

describe("preview", () => {
  it("normalizes the server shape", () => {
    const p = normPreview({
      ranking: "embeddings",
      skill_top_k: 2,
      tool_retrieval: { enabled: false, top_k: 6, always_include: [] },
      skills: {
        always: [{ name: "voice", chars: 70, source: "acme" }],
        on_demand: [{ name: "sync", score: 0.61, full: true, source: "mobile" }, { name: "refund", score: 0.1, full: false }],
        moved: [],
      },
      tools: [{ name: "ticket_get", title: "Read a ticket", score: 0.5, shown: true, pinned: false, approval: "read", source: "baseline" }],
      prompt: { chars: 1200, agent_chars: 300, skills_chars: 70, context_chars: 200, budgets: { prompt_chars: 12000, skills_total_chars: 8000, context_chars: 6000 } },
      warnings: [],
    });
    expect(p.ranking).toBe("embeddings");
    expect(p.onDemand.map((s) => s.full)).toEqual([true, false]);
    expect(p.tools[0].approval).toBe("read");
    expect(p.budgets.skills_total_chars).toBe(8000);
    expect(p.retrieval.enabled).toBe(false);
    expect(p.tools[0].source).toBe("baseline");
    expect(p.agentChars).toBe(300);
  });
  it("survives an empty body", () => {
    const p = normPreview({});
    expect(p.tools).toEqual([]);
    expect(p.skillTopK).toBeNull();
    expect(p.ranking).toBe("words");
  });
});

describe("tool usage", () => {
  it("keeps numeric fields only", () => {
    const u = normToolUsage({ runs: 3, tools: { a: { calls: 4, failures: "x", refused: 1, seconds: 0.5, runs: 2 } } });
    expect(u.runs).toBe(3);
    expect(u.tools.a).toEqual({ calls: 4, failures: 0, refused: 1, seconds: 0.5, runs: 2 });
  });
});
