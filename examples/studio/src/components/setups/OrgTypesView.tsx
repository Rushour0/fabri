import { useState } from "react";
import { normOrgType, packCompanies, packScope, setupsApi, type ScopeTree } from "../../lib/setups";
import { ItemsPanel } from "./ItemsPanel";
import { Empty, ErrorBox, Loading, TabBar, useLoad } from "./shared";
import { useLabel } from "./uiConfig";

type Sub = "context" | "skills";

// An agent type is the kind of organization served: a pack (packs/<name>/) whose context and skills reach every worker.
export function OrgTypesView({ tree, loading, error, onRetry }: { tree: ScopeTree | null; loading: boolean; error: string | null; onRetry: () => void }) {
  const packs = tree?.packs ?? [];
  const types = useLoad(
    () => Promise.all(packs.map(async (p) => normOrgType(p, await setupsApi.setupMeta(packScope(p).id).catch(() => ({}))))),
    packs.join(","),
    packs.length > 0,
  );
  const [sel, setSel] = useState<string | null>(null);
  const [sub, setSub] = useState<Sub>("context");
  const users = tree ? packCompanies(tree) : {};
  const list = types.data ?? packs.map((p) => normOrgType(p, {}));
  const cur = list.find((t) => t.name === sel);
  const label = useLabel();

  return (
    <div className="su-layout">
      <aside className="su-side" aria-label={label("agent_types")}>
        <div className="su-tree__head">
          <h3 className="su-side-title">{label("agent_types")}</h3>
        </div>
        {loading && !tree && <Loading what={label("agent_types").toLowerCase()} />}
        {error && <ErrorBox message={error} onRetry={onRetry} />}
        {types.error && <ErrorBox message={types.error} onRetry={types.reload} />}
        {tree && packs.length === 0 && <Empty title={`No ${label("agent_types").toLowerCase()}`}>Add a pack under <code>packs/</code>, for example <code>packs/saas/</code>.</Empty>}
        <ul className="su-cards">
          {list.map((t) => (
            <li key={t.name}>
              <button className={"su-typecard" + (t.name === sel ? " su-typecard--on" : "")} onClick={() => { setSel(t.name); setSub("context"); }} aria-pressed={t.name === sel}>
                <span className="su-typecard__name">{t.title}</span>
                <span className="su-dim">{(users[t.name] ?? []).length} compan{(users[t.name] ?? []).length === 1 ? "y" : "ies"}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="su-main">
        {!cur && <Empty title={`Pick a ${label("agent_type").toLowerCase()}`}>Each one is a kind of organization. Its context and {label("skills").toLowerCase()} reach every {label("worker").toLowerCase()} of the {label("companies").toLowerCase()} that pick it.</Empty>}
        {cur && (
          <>
            <div className="su-main__head">
              <h2 className="su-h2">{cur.title}</h2>
              <TabBar label={`${cur.title} sections`} value={sub} onChange={setSub} tabs={[{ id: "context", label: label("context") }, { id: "skills", label: label("skills") }]} />
            </div>
            <section className="su-card" aria-label={`About this ${label("agent_type").toLowerCase()}`}>
              {cur.description && <p className="su-panel__sub">{cur.description}</p>}
              <p className="su-panel__sub">
                Stored in <code>packs/{cur.name}/</code>. Used by{" "}
                {(users[cur.name] ?? []).length ? (users[cur.name] ?? []).map((o, i) => <span key={o}>{i ? ", " : ""}<code>{o}</code></span>) : `no ${label("company").toLowerCase()} yet`}.
              </p>
            </section>
            <ItemsPanel key={`${cur.name}|${sub}`} scope={packScope(cur.name)} kind={sub} localFirst />
          </>
        )}
      </div>
    </div>
  );
}
