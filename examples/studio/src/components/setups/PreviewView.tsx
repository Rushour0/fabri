import { useState } from "react";
import { setupsApi, type Preview, type ScopeTree } from "../../lib/setups";
import { ApprovalBadge, Empty, ErrorBox, LayerBadge, useLoad } from "./shared";
import { useLabel } from "./uiConfig";

function Meter({ label, used, budget }: { label: string; used: number; budget: number }) {
  const pct = budget > 0 ? Math.min(100, Math.round((used / budget) * 100)) : 0;
  const over = budget > 0 && used > budget;
  return (
    <div className="su-meter" aria-label={`${label}: ${used} of ${budget} characters`}>
      <div className="su-meter__head">
        <span>{label}</span>
        <span className={over ? "su-meter__n su-meter__n--over" : "su-meter__n"}>
          {used.toLocaleString()} / {budget.toLocaleString()}
        </span>
      </div>
      <div className="su-meter__track"><div className={over ? "su-meter__fill su-meter__fill--over" : "su-meter__fill"} style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

function Score({ value, max }: { value: number; max: number }) {
  const pct = max > 0 ? Math.max(4, Math.round((value / max) * 100)) : 0;
  return (
    <span className="su-score" title={String(value)}>
      <span className="su-score__bar" style={{ width: `${Math.round(pct * 0.56)}px` }} />
      <span className="su-score__n">{value.toFixed(value >= 10 || Number.isInteger(value) ? 0 : 2)}</span>
    </span>
  );
}

function Result({ p }: { p: Preview }) {
  const label = useLabel();
  const skillMax = Math.max(0, ...p.onDemand.map((s) => s.score));
  const toolMax = Math.max(0, ...p.tools.map((t) => t.score));
  const shownTools = p.tools.filter((t) => t.shown).length;
  const toolNote = p.retrieval.enabled
    ? `${shownTools} of ${p.tools.length} shown to the model: the top ${p.retrieval.topK ?? "?"} plus pinned tools`
    : `all ${p.tools.length} shown to the model; tool retrieval is off`;
  const fullSkills = p.onDemand.filter((s) => s.full).length;
  return (
    <>
      <p className="su-panel__sub">
        Ranked by {p.ranking === "embeddings" ? "embedding similarity" : "shared words (install the embeddings extra for semantic ranking)"}.
      </p>
      <div className="su-meters">
        <Meter label={`${label("worker")} prompt`} used={p.agentChars} budget={p.budgets.prompt_chars} />
        <Meter label="Always-loaded skills" used={p.skillsChars} budget={p.budgets.skills_total_chars} />
        <Meter label={label("context")} used={p.contextChars} budget={p.budgets.context_chars} />
      </div>
      <p className="su-dim">Whole system prompt: {p.promptChars.toLocaleString()} characters.</p>
      {p.warnings.length > 0 && (
        <ul className="su-lint__list" aria-label="Warnings">
          {p.warnings.map((w, i) => (
            <li key={i} className="su-lint__item su-lint__item--warning"><span className="su-lint__sev">warning</span><span className="su-lint__msg">{w}</span></li>
          ))}
        </ul>
      )}
      <div className="su-preview-grid">
        <section className="su-card" aria-label="Skills in the prompt">
          <h4 className="su-card__h">In the prompt <span className="su-dim">{p.always.length}</span></h4>
          {p.always.length === 0 ? <p className="su-dim">No always-loaded {label("skills").toLowerCase()}.</p> : (
            <ul className="su-reslist">
              {p.always.map((s) => (
                <li key={s.name} className="su-reslist__item"><code>{s.name}</code><LayerBadge source={s.source} /><span className="su-reslist__detail">{s.chars} chars</span></li>
              ))}
            </ul>
          )}
        </section>
        <section className="su-card" aria-label="Skills on demand">
          <h4 className="su-card__h">
            On demand <span className="su-dim">{fullSkills} listed in full{p.skillTopK === null ? "" : `, top ${p.skillTopK}`}</span>
          </h4>
          {p.onDemand.length === 0 ? <p className="su-dim">No on-demand {label("skills").toLowerCase()} for this {label("worker").toLowerCase()}.</p> : (
            <ol className="su-ranklist">
              {p.onDemand.map((s, i) => (
                <li key={s.name} className={"su-rankrow" + (s.full ? "" : " su-rankrow--dim")}>
                  <span className="su-rankrow__n" aria-hidden>{i + 1}</span>
                  <span className="su-rankrow__name">
                    <code>{s.name}</code>
                    <span className="su-rankrow__badges">
                      <span className={"su-badge " + (s.full ? "su-badge--on" : "su-badge--off")}>{s.full ? "full line" : "name only"}</span>
                      <LayerBadge source={s.source} />
                    </span>
                  </span>
                  <Score value={s.score} max={skillMax} />
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>
      {p.moved.length > 0 && (
        <p className="su-note">Moved to on demand by budget: {p.moved.map((m) => `${m.name} (${m.reason})`).join("; ")}</p>
      )}
      <section aria-label="Tool ranking">
        <h3 className="su-h3">{label("tools")} <span className="su-dim">{toolNote}</span></h3>
        {p.tools.length === 0 ? <Empty title="No tools">This {label("worker").toLowerCase()} has no tools in the merged setup.</Empty> : (
          <div className="su-scroll-x">
            <table className="su-table" aria-label="Ranked tools">
              <thead><tr><th scope="col">#</th><th scope="col">Name</th><th scope="col">Title</th><th scope="col">Approval</th><th scope="col">Layer</th><th scope="col">Score</th><th scope="col">Shown</th></tr></thead>
              <tbody>
                {p.tools.map((t, i) => (
                  <tr key={t.name} className={t.shown ? "" : "su-row--dim"}>
                    <td className="su-num">{i + 1}</td>
                    <th scope="row"><code>{t.name}</code>{t.pinned && <span className="su-badge su-badge--on su-badge--gap">pinned</span>}</th>
                    <td>{t.title || <span className="su-dim">-</span>}</td>
                    <td>{t.approval ? <ApprovalBadge approval={t.approval} /> : <span className="su-dim">-</span>}</td>
                    <td>{t.source ? <LayerBadge source={t.source} /> : <span className="su-dim">built in</span>}</td>
                    <td><Score value={t.score} max={toolMax} /></td>
                    <td>{t.shown ? "yes" : <span className="su-dim">find_tools</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}

/** Type a task and see which skills and tools one agent type would get. */
export function PreviewView({ tree, initial }: { tree: ScopeTree | null; initial?: { org?: string; project?: string } }) {
  const label = useLabel();
  const [org, setOrg] = useState(initial?.org ?? "");
  const [project, setProject] = useState(initial?.project ?? "");
  const [type, setType] = useState("");
  const [task, setTask] = useState("");
  const [skillTopK, setSkillTopK] = useState("");
  const [toolTopK, setToolTopK] = useState("");
  const [asked, setAsked] = useState<{ key: string; run: () => Promise<Preview> } | null>(null);
  const [nonce, setNonce] = useState(0);
  const projects = tree?.orgs.find((o) => o.name === org)?.projects ?? [];
  const types = useLoad(() => setupsApi.resolved(org || undefined, project || undefined), `ptypes|${org}|${project}`);
  const typeNames = types.data?.groups.agents.map((a) => a.name) ?? [];
  const pickedType = type || typeNames[0] || "";
  // The nonce makes Rank re-run an unchanged query, so edited setup files show up.
  const result = useLoad(() => (asked ? asked.run() : Promise.resolve(null as unknown as Preview)), `${asked?.key ?? "none"}|${nonce}`, !!asked);

  const submit = () => {
    if (!pickedType || !task.trim()) return;
    const q = {
      org: org || undefined,
      project: project || undefined,
      type: pickedType,
      task: task.trim(),
      skillTopK: Number(skillTopK) || undefined,
      toolTopK: Number(toolTopK) || undefined,
    };
    setAsked({ key: JSON.stringify(q), run: () => setupsApi.preview(q) });
    setNonce((n) => n + 1);
  };

  return (
    <div className="su-panel">
      <div className="su-panel__bar">
        <p className="su-panel__sub">
          Pick where the agent runs and type a task. You see which {label("skills").toLowerCase()} sit in the prompt, which on-demand ones
          get a full line, and which {label("tools").toLowerCase()} the model is shown, in rank order.
        </p>
      </div>
      <form className="su-preview-form" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        <div className="su-pickers">
          <div className="su-field">
            <label className="su-field__label" htmlFor="su-p-org">{label("company")}</label>
            <select id="su-p-org" className="su-input" value={org} onChange={(e) => { setOrg(e.target.value); setProject(""); setType(""); }}>
              <option value="">(none)</option>
              {tree?.orgs.map((o) => <option key={o.name} value={o.name}>{o.name}</option>)}
            </select>
          </div>
          <div className="su-field">
            <label className="su-field__label" htmlFor="su-p-proj">{label("project")}</label>
            <select id="su-p-proj" className="su-input" value={project} onChange={(e) => setProject(e.target.value)} disabled={!org}>
              <option value="">(none)</option>
              {projects.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
          </div>
          <div className="su-field">
            <label className="su-field__label" htmlFor="su-p-type">{label("worker")}</label>
            <select id="su-p-type" className="su-input" value={pickedType} onChange={(e) => setType(e.target.value)} disabled={typeNames.length === 0}>
              {typeNames.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </div>
          <div className="su-field su-field--narrow">
            <label className="su-field__label" htmlFor="su-p-sk">Skill lines</label>
            <input id="su-p-sk" className="su-input" inputMode="numeric" placeholder="setup" value={skillTopK} onChange={(e) => setSkillTopK(e.target.value.replace(/\D/g, ""))} />
          </div>
          <div className="su-field su-field--narrow">
            <label className="su-field__label" htmlFor="su-p-tk">Retrieval top k</label>
            <input id="su-p-tk" className="su-input" inputMode="numeric" placeholder="all" value={toolTopK} onChange={(e) => setToolTopK(e.target.value.replace(/\D/g, ""))} />
          </div>
        </div>
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-p-task">Task</label>
          <textarea id="su-p-task" className="su-input su-preview-task" rows={2} value={task} placeholder="The Android app shows duplicate edits after going offline"
            onChange={(e) => setTask(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); }} />
        </div>
        <div className="su-create__actions">
          <button type="submit" className="btn btn--primary" disabled={!pickedType || !task.trim()}>Rank</button>
        </div>
      </form>
      {types.error && <ErrorBox message={types.error} onRetry={types.reload} />}
      {result.error && <ErrorBox message={result.error} onRetry={result.reload} />}
      {asked && result.loading && !result.data && <p className="su-dim" role="status">Ranking...</p>}
      {result.data && <Result p={result.data} />}
    </div>
  );
}
