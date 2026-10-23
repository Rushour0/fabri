import { useMemo, useState } from "react";
import { errorText, setupsApi, validName, type ImportReport, type Scope } from "../../lib/setups";
import { ErrorBox } from "./shared";

type Format = "skill-md" | "fabri";

interface Picked {
  path: string;
  text: string;
}

function Report({ r }: { r: ImportReport }) {
  const rows: [string, string[]][] = [
    ["Created", r.created],
    ["Updated", r.updated],
    ["Skipped", r.skipped],
  ];
  return (
    <div className="su-report" role="status" aria-label={r.dry_run ? "Dry run result" : "Import result"}>
      <p className="su-report__head">{r.dry_run ? "Dry run. Nothing was written." : "Import applied."}</p>
      <dl className="su-report__grid">
        {rows.map(([label, names]) => (
          <div key={label} className="su-report__cell">
            <dt>{label}</dt>
            <dd>
              <span className="su-report__n">{names.length}</span>
              {names.length > 0 && <span className="su-report__names">{names.join(", ")}</span>}
            </dd>
          </div>
        ))}
        <div className="su-report__cell">
          <dt>Invalid</dt>
          <dd>
            <span className={"su-report__n" + (r.invalid.length ? " su-report__n--err" : "")}>{r.invalid.length}</span>
          </dd>
        </div>
      </dl>
      {r.invalid.length > 0 && (
        <ul className="su-lint__list">
          {r.invalid.map((x, i) => (
            <li key={i} className="su-lint__item su-lint__item--error">
              <span className="su-lint__sev">invalid</span>
              <span className="su-lint__msg">
                <code className="su-lint__where">{x.name}</code> {x.reason}
              </span>
            </li>
          ))}
        </ul>
      )}
      {r.warnings.length > 0 && (
        <ul className="su-lint__list">
          {r.warnings.map((w, i) => (
            <li key={i} className="su-lint__item su-lint__item--warning">
              <span className="su-lint__sev">warning</span>
              <span className="su-lint__msg">{w}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** Paste or upload SKILL.md files. Apply is blocked until a dry run of the same input has run. */
export function ImportPanel({
  scope,
  onApplied,
  onClose,
}: {
  scope: Scope;
  onApplied: () => void;
  onClose: () => void;
}) {
  const [format, setFormat] = useState<Format>("skill-md");
  const [pasted, setPasted] = useState("");
  const [slug, setSlug] = useState("pasted-skill");
  const [uploads, setUploads] = useState<Picked[]>([]);
  const [report, setReport] = useState<ImportReport | null>(null);
  const [dryKey, setDryKey] = useState<string | null>(null);
  const [busy, setBusy] = useState<null | "dry" | "apply">(null);
  const [error, setError] = useState<string | null>(null);

  const files = useMemo(() => {
    const out: Record<string, string> = {};
    if (pasted.trim() && validName(slug)) out[`${slug}/${format === "skill-md" ? "SKILL.md" : "skill.yaml"}`] = pasted;
    uploads.forEach((u, i) => {
      let path = u.path;
      // A bare SKILL.md has no folder to name the skill. Give it one.
      if (!path.includes("/")) path = `${path.replace(/\.[a-z]+$/i, "").toLowerCase() === "skill" ? `skill-${i + 1}` : path.replace(/\.[a-z]+$/i, "").toLowerCase()}/${path.toLowerCase() === "skill.md" ? "SKILL.md" : path}`;
      out[path] = u.text;
    });
    return out;
  }, [pasted, slug, uploads, format]);

  const count = Object.keys(files).length;
  const key = JSON.stringify([scope.id, format, files]);
  const fresh = dryKey === key;

  const pick = async (list: FileList | null) => {
    if (!list) return;
    const next: Picked[] = [];
    for (const f of Array.from(list)) {
      const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
      next.push({ path: rel, text: await f.text() });
    }
    setUploads(next);
  };

  const run = async (dry: boolean) => {
    setBusy(dry ? "dry" : "apply");
    setError(null);
    try {
      const r = await setupsApi.importItems(scope.id, format, files, dry);
      setReport(r);
      if (dry) setDryKey(key);
      else {
        setDryKey(null);
        onApplied();
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="su-import" aria-label="Import skills">
      <header className="su-editor__head">
        <h3 className="su-h3">Import into {scope.label}</h3>
        <button className="btn" onClick={onClose}>Close</button>
      </header>
      <div className="su-grid2">
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-imp-format">Format</label>
          <select id="su-imp-format" className="su-input" value={format} onChange={(e) => setFormat(e.target.value as Format)}>
            <option value="skill-md">SKILL.md</option>
            <option value="fabri">fabri skill folder</option>
          </select>
        </div>
        <div className="su-field">
          <label className="su-field__label" htmlFor="su-imp-slug">Name for the pasted skill</label>
          <input id="su-imp-slug" className="su-input" value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} aria-invalid={!validName(slug)} />
        </div>
      </div>
      <div className="su-field">
        <label className="su-field__label" htmlFor="su-imp-paste">Paste {format === "skill-md" ? "a SKILL.md" : "a skill.yaml"}</label>
        <textarea
          id="su-imp-paste"
          className="launcher__input su-code"
          rows={8}
          wrap="off"
          value={pasted}
          onChange={(e) => setPasted(e.target.value)}
          placeholder={"---\nname: my-skill\ndescription: When to use it.\n---\n\nSteps..."}
        />
      </div>
      <div className="su-field">
        <label className="su-field__label" htmlFor="su-imp-files">Or upload files (pick a folder, or several SKILL.md)</label>
        <input id="su-imp-files" type="file" multiple accept=".md,.yaml,.yml,.txt" onChange={(e) => pick(e.target.files)} />
        {uploads.length > 0 && <p className="su-dim">{uploads.length} file{uploads.length === 1 ? "" : "s"} picked.</p>}
      </div>

      {error && <ErrorBox message={error} />}
      {report && <Report r={report} />}

      <div className="su-actions">
        <button className="btn" onClick={() => run(true)} disabled={!count || busy !== null}>
          {busy === "dry" ? "Checking…" : "Dry run"}
        </button>
        <button
          className="btn btn--primary"
          onClick={() => run(false)}
          disabled={!fresh || busy !== null || (report?.invalid.length ?? 0) > 0 && (report?.created.length ?? 0) + (report?.updated.length ?? 0) === 0}
          title={fresh ? "" : "Run a dry run on this input first"}
        >
          {busy === "apply" ? "Importing…" : "Import"}
        </button>
        <span className="su-dim">{count} file{count === 1 ? "" : "s"}{fresh ? " . dry run done" : ""}</span>
      </div>
    </section>
  );
}
