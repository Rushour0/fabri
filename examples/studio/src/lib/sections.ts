import type { Surface } from "../hooks/useHashRoute";

// Which top-level Studio sections to show. Set VITE_STUDIO_SECTIONS to a comma
// separated list (for example "setups,settings") to hide the rest. Unset or
// empty shows everything, which is what a generic Studio wants.

export type Section = "roster" | "conversation" | "company" | "questions" | "fleet" | "setups" | "spend" | "settings";

export const SECTIONS: readonly Section[] = ["roster", "conversation", "company", "questions", "fleet", "setups", "spend", "settings"];

const ALIASES: Record<string, Section> = { catalog: "roster" };

export interface SectionConfig {
  /** null means every section is on */
  only: ReadonlySet<Section> | null;
}

export function parseSections(raw: string | undefined | null): SectionConfig {
  const names = (raw ?? "")
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean)
    .map((s) => ALIASES[s] ?? s)
    .filter((s): s is Section => (SECTIONS as readonly string[]).includes(s));
  return { only: names.length ? new Set(names) : null };
}

export function hasSection(cfg: SectionConfig, s: Section): boolean {
  return cfg.only === null || cfg.only.has(s);
}

/** The section a surface belongs to. Replay and history ride on the run views. */
export function surfaceSection(surface: Surface): Section | null {
  switch (surface) {
    case "catalog":
      return "roster";
    case "conversation":
    case "company":
    case "questions":
    case "fleet":
    case "setups":
    case "spend":
    case "settings":
      return surface;
    default:
      return null;
  }
}

/** Runs exist only with Conversation, Questions or Fleet; replay and history need one of them. */
export function runsVisible(cfg: SectionConfig): boolean {
  return hasSection(cfg, "conversation") || hasSection(cfg, "questions") || hasSection(cfg, "fleet");
}

export function surfaceAllowed(cfg: SectionConfig, surface: Surface): boolean {
  const s = surfaceSection(surface);
  if (s) return hasSection(cfg, s);
  return runsVisible(cfg);
}

/** Where to land: Conversation when shown, else Setups, else the first shown section. */
export function landingSurface(cfg: SectionConfig): Surface {
  if (hasSection(cfg, "conversation")) return "conversation";
  if (hasSection(cfg, "setups")) return "setups";
  const first = SECTIONS.find((s) => hasSection(cfg, s)) ?? "conversation";
  return first === "roster" ? "catalog" : first;
}

export const STUDIO_SECTIONS: SectionConfig = parseSections(import.meta.env.VITE_STUDIO_SECTIONS);
