import { describe, it, expect } from "vitest";
import { hasSection, landingSurface, parseSections, runsVisible, surfaceAllowed } from "./sections";

describe("studio sections", () => {
  it("shows everything when unset or empty", () => {
    for (const raw of [undefined, "", " , "]) {
      const cfg = parseSections(raw);
      expect(cfg.only).toBeNull();
      expect(hasSection(cfg, "fleet")).toBe(true);
      expect(landingSurface(cfg)).toBe("conversation");
    }
  });
  it("limits to the listed sections and lands on Setups", () => {
    const cfg = parseSections(" Setups, settings ,bogus");
    expect([...(cfg.only ?? [])]).toEqual(["setups", "settings"]);
    expect(hasSection(cfg, "conversation")).toBe(false);
    expect(landingSurface(cfg)).toBe("setups");
    expect(surfaceAllowed(cfg, "setups")).toBe(true);
    expect(surfaceAllowed(cfg, "questions")).toBe(false);
    expect(surfaceAllowed(cfg, "replay")).toBe(false);
    expect(runsVisible(cfg)).toBe(false);
  });
  it("keeps replay when a run view is shown", () => {
    const cfg = parseSections("fleet,setups");
    expect(surfaceAllowed(cfg, "replay")).toBe(true);
    expect(landingSurface(cfg)).toBe("setups");
  });
  it("knows the spend section", () => {
    const cfg = parseSections("setups,spend,settings");
    expect(surfaceAllowed(cfg, "spend")).toBe(true);
    expect(surfaceAllowed(cfg, "fleet")).toBe(false);
  });
  it("maps catalog to roster", () => {
    const cfg = parseSections("catalog");
    expect(surfaceAllowed(cfg, "catalog")).toBe(true);
    expect(landingSurface(cfg)).toBe("catalog");
  });
});
