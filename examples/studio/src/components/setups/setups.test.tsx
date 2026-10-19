import { describe, it, expect } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ApprovalBadge, Empty, ErrorBox, isInherited, LayerBadge, LintList, TabBar } from "./shared";
import { CatalogTable, ProbeView, SyncDiffView } from "./ToolsPanel";

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
  it("renders the catalog table columns", () => {
    const html = renderToStaticMarkup(
      <CatalogTable entries={[{ name: "t", alias: ["a"], title: "Title", description: "", approval: "read", source: "crm", layer: "baseline", origin: "registered", endpoint: "GET /x", kind: "http", options: {}, params: 0 }]} />,
    );
    for (const h of ["Title", "Name", "Alias", "Approval", "Source", "Endpoint"]) expect(html).toContain(h);
    expect(html).toContain("GET /x");
  });
  it("renders sync diff and probe output", () => {
    expect(renderToStaticMarkup(<SyncDiffView diff={{ added: ["n"], removed: [], changed: ["c"] }} />)).toContain("Added");
    expect(renderToStaticMarkup(<ProbeView r={{ probed: ["g"], skipped: [{ name: "p", reason: "not a GET" }] }} />)).toContain("not a GET");
  });
});
