import { useEffect, useState } from "react";

export const SECTIONS = [
  { id: "overview", label: "Today" },
  { id: "opportunities", label: "Opportunities" },
  { id: "network", label: "Network" },
  { id: "negotiations", label: "Negotiations" },
  { id: "plan", label: "Plan & approvals" },
  { id: "policy", label: "Policy" },
  { id: "audit", label: "Audit" },
] as const;

export type SectionId = (typeof SECTIONS)[number]["id"];

export type Route = { merchant: string | null; section: SectionId };

export function parseHash(hash: string): Route {
  const m = /^#\/m\/([a-z0-9_.:-]+)(?:\/([a-z]+))?/.exec(hash);
  const section = SECTIONS.find((s) => s.id === m?.[2])?.id ?? "overview";
  return { merchant: m?.[1] ?? null, section };
}

export function href(merchant: string, section: SectionId): string {
  return `#/m/${merchant}/${section}`;
}

export function useRoute(): Route {
  const [route, setRoute] = useState(() => parseHash(window.location.hash));
  useEffect(() => {
    const on = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return route;
}
