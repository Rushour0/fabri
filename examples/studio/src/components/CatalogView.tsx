import type { Catalog, CatalogAgency, CatalogCompany } from "../lib/api";
import type { Company } from "../lib/api";

// The roster front door (Studio `--catalog` mode): browse the agencies and
// multi-level companies on offer and hire one. Picking an entry makes it the
// active run target; a company additionally lights up the Company org-chart.

export interface CatalogSelection {
  ref: string;
  kind: "agency" | "company";
  title: string;
  org?: Company;
}

function money(v: number | undefined): string {
  return typeof v === "number" ? `$${v}/run` : "";
}

function AgencyCard({ a, onRun }: { a: CatalogAgency; onRun: (s: CatalogSelection) => void }) {
  return (
    <article className="cat-card">
      <div className="cat-card__top">
        {a.category && <span className="cat-card__tag">{a.category}</span>}
        {a.self_improving && <span className="cat-card__mark">Self-improving</span>}
      </div>
      <h3 className="cat-card__title">{a.title}</h3>
      {a.tagline && <p className="cat-card__tagline">{a.tagline}</p>}
      <div className="cat-card__meta">
        {a.agents ?? 0} agents · {a.tools ?? 0} tools{a.max_cost_usd != null && ` · ${money(a.max_cost_usd)}`}
      </div>
      <button className="btn btn--primary cat-card__run" onClick={() => onRun({ ref: a.ref, kind: "agency", title: a.title })}>
        Run this agency
      </button>
    </article>
  );
}

function CompanyCard({ c, onRun }: { c: CatalogCompany; onRun: (s: CatalogSelection) => void }) {
  return (
    <article className="cat-card cat-card--company">
      <div className="cat-card__top">
        <span className="cat-card__tag cat-card__tag--company">Company</span>
      </div>
      <h3 className="cat-card__title">{c.title}</h3>
      {c.positioning && <p className="cat-card__tagline">{c.positioning}</p>}
      <div className="cat-card__meta">
        {c.node_count ?? 0} agents across {c.member_agencies?.length ?? 0} crews
        {c.max_cost_usd != null && ` · ${money(c.max_cost_usd)}`}
      </div>
      {c.member_agencies && c.member_agencies.length > 0 && (
        <div className="cat-card__chips">
          {c.member_agencies.map((m) => (
            <span className="cat-card__chip" key={m}>
              {m}
            </span>
          ))}
        </div>
      )}
      <button
        className="btn btn--primary cat-card__run"
        onClick={() => onRun({ ref: c.ref, kind: "company", title: c.title, org: c.org })}
      >
        Run this company
      </button>
    </article>
  );
}

export function CatalogView({ catalog, onRun }: { catalog: Catalog; onRun: (s: CatalogSelection) => void }) {
  const hasCompanies = catalog.companies.length > 0;
  return (
    <section className="catalog" aria-label="Roster catalog">
      <div className="catalog__head">
        <h2 className="catalog__title">The Roster</h2>
        <p className="catalog__sub">
          Small, self-improving agencies — and multi-level companies — you can hire in one click. Pick one, give it a
          task, and it runs with the cost shown. When it needs a human, it asks in the Questions tab.
        </p>
      </div>

      {hasCompanies && (
        <>
          <h3 className="catalog__section">Companies</h3>
          <div className="catalog__grid">
            {catalog.companies.map((c) => (
              <CompanyCard key={c.ref} c={c} onRun={onRun} />
            ))}
          </div>
        </>
      )}

      <h3 className="catalog__section">Agencies</h3>
      <div className="catalog__grid">
        {catalog.agencies.map((a) => (
          <AgencyCard key={a.ref} a={a} onRun={onRun} />
        ))}
      </div>
    </section>
  );
}
