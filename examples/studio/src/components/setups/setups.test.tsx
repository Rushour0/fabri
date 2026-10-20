import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApprovalBadge, Empty, ErrorBox, isInherited, LayerBadge, LintList, TabBar } from "./shared";
import { CatalogTable, ProbeView, SyncDiffView } from "./ToolsPanel";
import { normCatalog } from "../../lib/setups";

// No DOM test runner is installed, so these render to static markup.

describe("shared pieces", () => {
  it("shows the source layer on a badge", () => {
    expect(renderToStaticMarkup(<LayerBadge source="org:acme" />)).toContain("org:acme");
  });
  it("marks inherited items", () => {
    expect(isInherited("baseline", "org:acme")).toBe(true);
    expect(isInherited("org:acme", "org:acme")).toBe(false);
    expect(isInherited("acme", "org:acme")).toBe(false);
  });
  it("renders approval classes", () => {
    expect(renderToStaticMarkup(<ApprovalBadge approval="always_approve" />)).toContain("always approve");
  });
  it("lists lint findings with counts, or a clean line", () => {
    const html = renderToStaticMarkup(<LintList findings={[{ severity: "error", kind: "skills", name: "a", message: "bad yaml" }]} />);
    expect(html).toContain("1 error");
    expect(html).toContain("bad yaml");
    expect(renderToStaticMarkup(<LintList findings={[]} />)).toContain("no findings");
  });
  it("renders empty and error states with roles", () => {
    expect(renderToStaticMarkup(<Empty title="Nothing" />)).toContain("Nothing");
    expect(renderToStaticMarkup(<ErrorBox message="boom" onRetry={() => {}} />)).toContain('role="alert"');
  });
  it("renders a tablist with one tab stop", () => {
    const html = renderToStaticMarkup(<TabBar label="x" value="b" onChange={() => {}} tabs={[{ id: "a", label: "A" }, { id: "b", label: "B" }]} />);
    expect(html).toContain('role="tablist"');
    expect(html.match(/tabindex="0"/g)?.length).toBe(1);
  });
});

describe("tools views", () => {
  it("renders the tools table with columns, a count and pager", () => {
    const entries = normCatalog(
      Array.from({ length: 30 }, (_, i) => ({
        name: `t${i}`, title: "Title", approval: "read", layer: "baseline", origin: "declared",
        endpoint: { method: "GET", path: `/x/${i}`, kind: "openapi" },
      })),
    );
    const html = renderToStaticMarkup(<CatalogTable entries={entries} scopeId="baseline" />);
    for (const h of ["Name", "Method", "Path", "Approval", "Enabled", "Layer"]) expect(html).toContain(`>${h}</th>`);
    expect(html).toContain("/x/0");
    expect(html).not.toContain("/x/25");
    expect(html).toContain("1-25 of 30");
    expect(html).toContain("Page 1 of 2");
  });
  it("renders sync diff and probe output", () => {
    expect(renderToStaticMarkup(<SyncDiffView diff={{ added: ["n"], removed: [], changed: ["c"] }} />)).toContain("Added");
    expect(renderToStaticMarkup(<ProbeView r={{ probed: ["g"], skipped: [{ name: "p", reason: "not a GET" }] }} />)).toContain("not a GET");
  });
});
