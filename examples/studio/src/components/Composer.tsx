import { useState } from "react";

// The task input. Submits a task to start (or continue) a thread. While a run is
// in flight the Run button becomes a Stop button (cancel), and the input is
// disabled; once the turn finishes the composer re-enables for a follow-up.
export function Composer({
  onSubmit,
  onCancel,
  busy,
  followup,
}: {
  onSubmit: (task: string) => void;
  onCancel?: () => void;
  busy: boolean;
  followup?: boolean;
}) {
  const [task, setTask] = useState("");

  const submit = () => {
    const t = task.trim();
    if (!t || busy) return;
    onSubmit(t);
    setTask("");
  };

  const placeholder = busy
    ? "A run is in progress…"
    : followup
      ? "Reply or ask a follow-up…"
      : "Ask the agency to do something…";

  return (
    <form
      className="composer"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <textarea
        className="composer__input"
        value={task}
        placeholder={placeholder}
        onChange={(e) => setTask(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
        disabled={busy}
        rows={2}
      />
      {busy && onCancel ? (
        <button className="btn composer__send" type="button" onClick={onCancel}>
          Stop
        </button>
      ) : (
        <button className="btn btn--primary composer__send" type="submit" disabled={busy || !task.trim()}>
          {followup ? "Send" : "Run"}
        </button>
      )}
    </form>
  );
}
