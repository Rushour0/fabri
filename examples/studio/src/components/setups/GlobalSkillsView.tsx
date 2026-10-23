import { useState } from "react";
import { BASELINE, normGlobalSkills, setupsApi, unlistedWorkers, type GlobalSkill } from "../../lib/setups";
import { AgentTypesView } from "./AgentTypesView";
import { Empty, ErrorBox, Loading, useLoad } from "./shared";

export function GlobalSkillsTable({ skills, others, onWorker }: { skills: GlobalSkill[]; others: string[]; onWorker?: (w: string) => void }) {
  return (
    <>
      <table className="su-table" aria-label="Global skills">
        <thead><tr><th scope="col">Global skill</th><th scope="col">What it does</th><th scope="col">Worker</th></tr></thead>
        <tbody>
          {skills.map((s) => (
            <tr key={s.name}>
              <th scope="row">{s.name}</th>
              <td>{s.what}</td>
              <td>
                {s.workers.map((w, i) => (
                  <span key={w}>
                    {i ? ", " : ""}
                    {onWorker ? <button className="su-link" onClick={() => onWorker(w)}><code>{w}</code></button> : <code>{w}</code>}
                  </span>
                ))}
                {s.workers.length === 0 && s.code && <span className="su-dim">code: <code>{s.code}</code></span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {others.length > 0 && (
        <p className="su-dim">
          Also running: {others.map((w, i) => <span key={w}>{i ? ", " : ""}<code>{w}</code></span>)} (routing and replies, not a skill of their own).
        </p>
      )}
    </>
  );
}

export function GlobalSkillsView() {
  const res = useLoad(async () => {
    const [meta, workers] = await Promise.all([setupsApi.setupMeta(BASELINE.id), setupsApi.items(BASELINE.id, "agents")]);
    const skills = normGlobalSkills(meta);
    return { skills, others: unlistedWorkers(skills, workers.map((w) => w.name)) };
  }, "global-skills");
  const [worker, setWorker] = useState<string | null>(null);

  return (
    <div className="su-main su-main--full">
      <section className="su-card" aria-label="Global skills">
        <h3 className="su-h3">Global skills</h3>
        <p className="su-panel__sub">
          Every agent type gets these. Listed in <code>baseline/setup.yaml</code> under <code>global_skills</code>; each is served by
          the workers named here.
        </p>
        {res.loading && !res.data && <Loading what="global skills" />}
        {res.error && <ErrorBox message={res.error} onRetry={res.reload} />}
        {res.data && res.data.skills.length === 0 && <Empty title="No global skills listed">Add a global_skills list to baseline/setup.yaml.</Empty>}
        {res.data && res.data.skills.length > 0 && <GlobalSkillsTable skills={res.data.skills} others={res.data.others} onWorker={setWorker} />}
      </section>
      <AgentTypesView key={worker ?? ""} initial={worker} />
    </div>
  );
}
