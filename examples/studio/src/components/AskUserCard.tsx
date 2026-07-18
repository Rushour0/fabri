import { useState, type ReactNode } from "react";
import type { FabriEvent } from "../lib/events";

// The manager pausing mid-run to ask the user something — the interactive core
// of Studio. Renders as a primary card (not a light activity line) because it
// demands an answer before the run continues.
//
// Contract adapted from ludexel AskUserCard: free-text OR options + default,
// answer POSTed back keyed by question_id. Studio answers one question per card;
// the parent submits sequentially when several are pending (answering one may
// unblock the next server-side).
export function AskUserCard({
  ev,
  onAnswer,
  disabled,
  header,
}: {
  ev: FabriEvent;
  onAnswer: (questionId: string, answer: string, selectedOption?: string) => Promise<void>;
  disabled?: boolean;
  // Optional slot rendered at the top of the card. The cross-run Questions
  // inbox uses it to name the source run + waiting age; the conversation
  // surface passes nothing, so its card is unchanged.
  header?: ReactNode;
}) {
  const options = Array.isArray(ev.options) ? ev.options : undefined;
  const [text, setText] = useState(ev.default ?? "");
  const [selected, setSelected] = useState<string | undefined>(
    options && ev.default && options.includes(ev.default) ? ev.default : undefined,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = options ? selected !== undefined : text.trim().length > 0;

  const submit = async () => {
    if (!ev.question_id || !canSubmit || submitting || disabled) return;
    setSubmitting(true);
    setError(null);
    try {
      const answer = options ? (selected as string) : text.trim();
      await onAnswer(ev.question_id, answer, options ? (selected as string) : undefined);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  };

  return (
    <div className={"ask" + (disabled ? " ask--waiting" : "")}>
      {header}
      <div className="ask__role">
        <span className="ask__dot" aria-hidden />
        <span className="ask__name">Manager needs your input</span>
      </div>
      <div className="ask__question">{ev.question}</div>

      {options ? (
        <div className="ask__options">
          {options.map((opt) => (
            <button
              key={opt}
              className={"ask__option" + (selected === opt ? " ask__option--sel" : "")}
              onClick={() => setSelected(opt)}
              disabled={submitting || disabled}
            >
              {opt}
            </button>
          ))}
        </div>
      ) : (
        <textarea
          className="ask__input"
          value={text}
          placeholder="Type your answer…"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit();
          }}
          disabled={submitting || disabled}
          rows={2}
        />
      )}

      {error && <div className="ask__error">{error}</div>}

      <div className="ask__actions">
        <button className="btn btn--primary" onClick={submit} disabled={!canSubmit || submitting || disabled}>
          {submitting ? "Sending…" : "Answer"}
        </button>
        {!options && <span className="ask__hint">⌘⏎ to send</span>}
      </div>
    </div>
  );
}
