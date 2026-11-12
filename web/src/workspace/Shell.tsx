import type { JSX } from "react";
import type { Membership, SessionBody } from "../api/types";
import { useAuth } from "../auth/AuthContext";
import { Badge } from "../components/ui";
import { Audit } from "./Audit";
import { Negotiations } from "./Negotiations";
import { NetworkPage } from "./Network";
import { Opportunities } from "./Opportunities";
import { Overview } from "./Overview";
import { PlanPage } from "./Plan";
import { PolicyPage } from "./Policy";
import { SECTIONS, href } from "./route";
import type { SectionId } from "./route";

export type PageProps = { merchant: string; membership: Membership };

const PAGES: Record<SectionId, (p: PageProps) => JSX.Element | null> = {
  overview: Overview,
  opportunities: Opportunities,
  network: NetworkPage,
  negotiations: Negotiations,
  plan: PlanPage,
  policy: PolicyPage,
  audit: Audit,
};

export function Shell({
  session,
  membership,
  section,
}: {
  session: SessionBody;
  membership: Membership;
  section: SectionId;
}) {
  const { logout } = useAuth();
  const Page = PAGES[section];
  const others = session.memberships.filter((m) => m.merchant_id !== membership.merchant_id);
  return (
    <div className="min-h-screen bg-slate-100 text-slate-900">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-lg font-semibold tracking-tight">Elide</span>
            <span className="text-sm text-slate-600">{membership.display_name}</span>
            {membership.is_synthetic && <Badge tone="info">SYNTHETIC MERCHANT</Badge>}
            <Badge>{membership.role}</Badge>
          </div>
          <div className="flex flex-wrap items-center gap-3 text-sm">
            {others.length > 0 && (
              <label className="flex items-center gap-2">
                <span className="sr-only">Switch merchant</span>
                <select
                  className="input w-auto"
                  value={membership.merchant_id}
                  onChange={(e) => window.location.assign(href(e.target.value, section))}
                >
                  {session.memberships.map((m) => (
                    <option key={m.merchant_id} value={m.merchant_id}>
                      {m.display_name}
                    </option>
                  ))}
                </select>
              </label>
            )}
            <span>{session.user.display_name}</span>
            {session.user.identity_label && <Badge tone="warn">{session.user.identity_label}</Badge>}
            <button type="button" className="btn-secondary" onClick={() => void logout()}>
              Sign out
            </button>
          </div>
        </div>
        <nav aria-label="Workspace" className="mx-auto max-w-6xl overflow-x-auto px-4">
          <ul className="flex gap-1">
            {SECTIONS.map((s) => (
              <li key={s.id}>
                <a
                  href={href(membership.merchant_id, s.id)}
                  aria-current={s.id === section ? "page" : undefined}
                  className={`inline-block whitespace-nowrap border-b-2 px-3 py-2 text-sm ${
                    s.id === section
                      ? "border-indigo-700 font-medium text-indigo-800"
                      : "border-transparent text-slate-600 hover:text-slate-900"
                  }`}
                >
                  {s.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <main id="main" className="mx-auto max-w-6xl space-y-4 px-4 py-6">
        <Page key={`${membership.merchant_id}:${section}`} merchant={membership.merchant_id} membership={membership} />
      </main>
    </div>
  );
}
