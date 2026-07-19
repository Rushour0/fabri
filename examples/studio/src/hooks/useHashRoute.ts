import { useCallback, useEffect, useRef, useState } from "react";

// The Studio's tab surfaces. Kept here (not in App) so the router owns the
// canonical list and hash parsing can validate against it.
export const SURFACES = [
  "catalog",
  "conversation",
  "company",
  "questions",
  "history",
  "fleet",
  "replay",
] as const;
export type Surface = (typeof SURFACES)[number];

export interface Route {
  surface: Surface;
  // Only meaningful for the "replay" surface: the run being replayed, so a
  // `#replay/<id>` link is deep-linkable and survives a reload.
  replayId: string | null;
}

function isSurface(value: string): value is Surface {
  return (SURFACES as readonly string[]).includes(value);
}

// `#conversation`, `#questions`, `#replay/<run-id>` -> Route. Tolerates a
// leading `#/` and returns null when the hash is empty or unrecognised so the
// caller can fall back to its default surface.
export function parseHash(hash: string): Route | null {
  const raw = hash.replace(/^#\/?/, "");
  if (!raw) return null;
  const [head, ...rest] = raw.split("/");
  if (!isSurface(head)) return null;
  if (head === "replay") {
    const id = rest.join("/");
    return { surface: "replay", replayId: id ? decodeURIComponent(id) : null };
  }
  return { surface: head, replayId: null };
}

export function toHash(surface: Surface, replayId: string | null): string {
  if (surface === "replay" && replayId) {
    return `#replay/${encodeURIComponent(replayId)}`;
  }
  return `#${surface}`;
}

// Tab state backed by the URL hash. No router dependency: it reads the initial
// hash, writes on navigation (which pushes a history entry so Back works), and
// listens for `hashchange` (browser Back/Forward + manual edits). `hadInitialHash`
// lets the caller decide whether an explicit deep-link should win over a
// mode-driven default (e.g. landing on the Roster in catalog mode).
export function useHashRoute(fallback: Surface): {
  surface: Surface;
  replayId: string | null;
  go: (surface: Surface, replayId?: string | null) => void;
  hadInitialHash: boolean;
} {
  const hadInitialHash = useRef(Boolean(window.location.hash));
  const [route, setRoute] = useState<Route>(
    () => parseHash(window.location.hash) ?? { surface: fallback, replayId: null },
  );

  useEffect(() => {
    const onHash = () => {
      const parsed = parseHash(window.location.hash);
      if (parsed) setRoute(parsed);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const go = useCallback((surface: Surface, replayId: string | null = null) => {
    setRoute({ surface, replayId });
    const next = toHash(surface, replayId);
    // Writing the hash fires `hashchange` (harmless re-set); guard the no-op so
    // re-selecting the current tab doesn't spam history entries.
    if (window.location.hash !== next) window.location.hash = next;
  }, []);

  return {
    surface: route.surface,
    replayId: route.replayId,
    go,
    hadInitialHash: hadInitialHash.current,
  };
}
