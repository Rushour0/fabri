import { useEffect, useMemo, useState } from "react";
import { BASELINE, setupsApi, skillFilesForType, skillTypes, type SetupItem } from "../../lib/setups";
import { Editor, NewItem } from "./ItemsPanel";
import { Empty, ErrorBox, LayerBadge, Loading, TabBar, useLoad } from "./shared";
import { useLabel } from "./uiConfig";

type Sub = "prompt" | "skills";

function GlobalSkills({ type }: { type: string }) {
  const res = useLoad(async () => {
    const all = await setupsApi.items(BASELINE.id, "skills");
    const details = await Promise.all(
      all.map(async (s) => {
        try {
          return { s, types: skillTypes(await setupsApi.item(BASELINE.id, "skills", s.name)) };
        } catch {
          return { s, types: [] as string[] };
        }
      }),
    );
    return {
      all,
      mine: details.filter((d) => d.types.includes(type)).map((d) => d.s),
      universal: details.filter((d) => d.types.length === 0).length,
    };
  }, type);
  const [open, setOpen] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    setOpen(null);
    setCreating(false);
  }, [type]);

  const existing = useMemo(() => new Set((res.data?.all ?? []).map((s) => s.name)), [res.data]);
  const mine: SetupItem[] = res.data?.mine ?? [];
  const openItem = mine.find((s) => s.name === open);

  return (
    <div className="su-panel">
      <div className="su-panel__bar">
        <p className="su-panel__sub">
          Skills for the <strong>{type}</strong> worker: baseline skills whose <code>types</code> include it. Every company and
          project inherits them.
        </p>
        <button className="btn btn--primary" onClick={() => setCreating((v) => !v)} aria-expanded={creating}>
          New skill
        </button>
      </div>
      {creating && (
        <NewItem
          scope={BASELINE}
          kind="skills"
          existing={existing}
          makeFiles={(n) => skillFilesForType(n, type)}
          onCancel={() => setCreating(false)}
          onCreated={(n) => {
            setCreating(false);
            res.reload();
            setOpen(n);
          }}
        />
      )}
      {res.loading && !res.data && <Loading what="skills" />}
      {res.error && <ErrorBox message={res.error} onRetry={res.reload} />}
      <div className="su-split">
        <div className="su-list-wrap">
          {res.data && mine.length === 0 && (
            <Empty title={`No baseline skills target ${type}`}>
              Add one here. {res.data.universal > 0 && `${res.data.universal} baseline skill${res.data.universal === 1 ? " applies" : "s apply"} to every type.`}
            </Empty>
          )}
          {mine.length > 0 && (
            <table className="su-table" aria-label={`Skills for ${type}`}>
              <thead><tr><th scope="col">Skill</th><th scope="col">Layer</th></tr></thead>
              <tbody>
                {mine.map((s) => (
                  <tr key={s.name} className={s.name === open ? "su-row--on" : undefined}>
                    <th scope="row">
                      <button className="su-link" onClick={() => setOpen(s.name === open ? null : s.name)} aria-expanded={s.name === open}>{s.name}</button>
                      {!s.enabled && <span className="su-badge su-badge--off">disabled</span>}
                    </th>
                    <td><LayerBadge source={s.source_layer || "baseline"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {res.data && res.data.universal > 0 && mine.length > 0 && (
            <p className="su-dim">{res.data.universal} more baseline skill{res.data.universal === 1 ? " applies" : "s apply"} to every type (see Advanced, baseline).</p>
          )}
        </div>
        {open && (
          <Editor key={open} scope={BASELINE} kind="skills" name={open} item={openItem} onClosed={() => setOpen(null)} onChanged={res.reload} />
        )}
      </div>
    </div>
  );
}

export function AgentTypesView({ initial = null }: { initial?: string | null }) {
  const types = useLoad(() => setupsApi.items(BASELINE.id, "agents"), "agent-types");
  const [sel, setSel] = useState<string | null>(initial);
  const [sub, setSub] = useState<Sub>("prompt");
  const [creating, setCreating] = useState(false);
  const list = types.data ?? [];
  const existing = useMemo(() => new Set(list.map((t) => t.name)), [list]);
  const cur = list.find((t) => t.name === sel);
  const label = useLabel();

  return (
    <div className="su-layout">
      <aside className="su-side" aria-label={label("workers")}>
        <div className="su-tree__head">
          <h3 className="su-side-title">{label("workers")}</h3>
          <button className="btn" onClick={() => setCreating((v) => !v)} aria-expanded={creating}>+ {label("worker")}</button>
        </div>
        {creating && (
          <NewItem
            scope={BASELINE}
            kind="agents"
            existing={existing}
            onCancel={() => setCreating(false)}
            onCreated={(n) => {
              setCreating(false);
              types.reload();
              setSel(n);
              setSub("prompt");
            }}
          />
        )}
        {types.loading && !types.data && <Loading what={label("workers").toLowerCase()} />}
        {types.error && <ErrorBox message={types.error} onRetry={types.reload} />}
        {types.data && list.length === 0 && <Empty title={`No ${label("workers").toLowerCase()}`}>Create one, for example <code>support</code> or <code>triage</code>.</Empty>}
        <ul className="su-cards">
          {list.map((t) => (
            <li key={t.name}>
              <button className={"su-typecard" + (t.name === sel ? " su-typecard--on" : "")} onClick={() => { setSel(t.name); setSub("prompt"); }} aria-pressed={t.name === sel}>
                <span className="su-typecard__name">{t.name}</span>
                {!t.enabled && <span className="su-badge su-badge--off">disabled</span>}
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <div className="su-main">
        {!sel && <Empty title={`Pick a ${label("worker").toLowerCase()}`}>Open one to edit its prompt and the {label("skills").toLowerCase()} every {label("company").toLowerCase()} inherits.</Empty>}
        {sel && (
          <>
            <div className="su-main__head">
              <h2 className="su-h2">{sel}</h2>
              <TabBar
                label={`${sel} sections`}
                value={sub}
                onChange={setSub}
                tabs={[{ id: "prompt", label: "Prompt and config" }, { id: "skills", label: "Skills" }]}
              />
            </div>
            {sub === "prompt" ? (
              <div className="su-panel">
                <p className="su-panel__sub">
                  Stored in <code>baseline/agents/{sel}/</code>. Files under <code>skills/</code> in that folder show up below too.
                </p>
                <Editor key={sel} scope={BASELINE} kind="agents" name={sel} item={cur} onClosed={() => setSel(null)} onChanged={types.reload} />
              </div>
            ) : (
              <GlobalSkills type={sel} />
            )}
          </>
        )}
      </div>
    </div>
  );
}
