import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { errorText, lintCounts, type LintFinding } from "../../lib/setups";

// Small pieces every Setups panel uses: load state, empty/error boxes, the
// source-layer badge, the approval badge and the inline lint list.

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
}

/** Run `fn` when `key` changes; ignore responses that arrive after a newer call. */
export function useLoad<T>(fn: () => Promise<T>, key: string, enabled = true): Loaded<T> {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({
    data: null,
    error: null,
    loading: enabled,
  });
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const seq = useRef(0);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    if (!enabled) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    const mine = ++seq.current;
    setState((s) => ({ ...s, loading: true, error: null }));
    fnRef.current().then(
      (data) => mine === seq.current && setState({ data, error: null, loading: false }),
      (e) => mine === seq.current && setState({ data: null, error: errorText(e), loading: false }),
    );
    return () => {
      seq.current++;
    };
  }, [key, tick, enabled]);

  return { ...state, reload };
}

export function Loading({ what }: { what: string }) {
  return (
    <div className="su-state" role="status" aria-live="polite">
      <span className="su-spinner" aria-hidden />
      Loading {what}…
    </div>
  );
}

export function ErrorBox({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div className="error-banner su-error" role="alert">
      <span>{message}</span>
      {onRetry && (
        <button className="btn su-error__retry" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="su-state su-state--empty">
      <p className="su-state__title">{title}</p>
      {children && <p className="su-state__sub">{children}</p>}
    </div>
  );
}

/** Which layer an item comes from. Muted when it is not the open scope. */
export function LayerBadge({ source, inherited = false }: { source: string; inherited?: boolean }) {
  if (!source) return <span className="su-badge su-badge--none">unknown</span>;
  const level = source.split(":")[0];
  const cls = ["baseline", "pack", "org", "project"].includes(level) ? level : "other";
  return (
    <span
      className={`su-badge su-badge--${cls}` + (inherited ? " su-badge--inherited" : "")}
      title={inherited ? `Inherited from ${source}` : `Defined in ${source}`}
    >
      {source}
    </span>
  );
}

export function ApprovalBadge({ approval }: { approval: string }) {
  if (!approval) return <span className="su-badge su-badge--none">-</span>;
  return <span className={`su-badge su-appr su-appr--${approval}`}>{approval.replace("_", " ")}</span>;
}

export function LintList({ findings, label = "Lint" }: { findings: LintFinding[]; label?: string }) {
  const c = lintCounts(findings);
  if (!findings.length) {
    return (
      <p className="su-lint su-lint--clean" role="status">
        {label}: no findings.
      </p>
    );
  }
  return (
    <div className="su-lint" role="status" aria-label={label}>
      <p className="su-lint__head">
        {label}: {c.errors} error{c.errors === 1 ? "" : "s"}, {c.warnings} warning{c.warnings === 1 ? "" : "s"}
      </p>
      <ul className="su-lint__list">
        {findings.map((f, i) => (
          <li key={i} className={`su-lint__item su-lint__item--${f.severity}`}>
            <span className="su-lint__sev">{f.severity}</span>
            <span className="su-lint__msg">
              {(f.kind || f.name) && (
                <code className="su-lint__where">{[f.kind, f.name].filter(Boolean).join("/")}</code>
              )}{" "}
              {f.message}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Loose compare of an item's source layer to the open scope. */
export function isInherited(source: string, scopeId: string): boolean {
  if (!source) return false;
  const key = (s: string) => s.trim().toLowerCase().split(/[:/]/).filter(Boolean).pop() ?? "";
  return source !== scopeId && key(source) !== key(scopeId);
}

/** Two-step button so a delete is never one stray click. */
export function ConfirmButton({
  label,
  confirmLabel,
  onConfirm,
  disabled,
}: {
  label: string;
  confirmLabel: string;
  onConfirm: () => void;
  disabled?: boolean;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = setTimeout(() => setArmed(false), 5000);
    return () => clearTimeout(t);
  }, [armed]);
  return armed ? (
    <span className="su-confirm" role="group" aria-label={confirmLabel}>
      <button className="btn su-btn--danger" onClick={() => { setArmed(false); onConfirm(); }} autoFocus>
        {confirmLabel}
      </button>
      <button className="btn" onClick={() => setArmed(false)}>
        Cancel
      </button>
    </span>
  ) : (
    <button className="btn su-btn--danger-quiet" onClick={() => setArmed(true)} disabled={disabled}>
      {label}
    </button>
  );
}

/** Tab strip with arrow-key movement. Reuses the header's tab look. */
export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  label,
}: {
  tabs: { id: T; label: string }[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  const onKey = (e: React.KeyboardEvent) => {
    const i = tabs.findIndex((t) => t.id === value);
    let n = i;
    if (e.key === "ArrowRight") n = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") n = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") n = 0;
    else if (e.key === "End") n = tabs.length - 1;
    else return;
    e.preventDefault();
    onChange(tabs[n].id);
    requestAnimationFrame(() => document.getElementById(`su-tab-${tabs[n].id}`)?.focus());
  };
  return (
    <div className="tabs su-tabs" role="tablist" aria-label={label} onKeyDown={onKey}>
      {tabs.map((t) => (
        <button
          key={t.id}
          id={`su-tab-${t.id}`}
          role="tab"
          aria-selected={t.id === value}
          tabIndex={t.id === value ? 0 : -1}
          className={"tab" + (t.id === value ? " tab--on" : "")}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}
