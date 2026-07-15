import { useState } from "react";

// The task input. In a single-run Studio there's one job: submit a task to
// start a run. It's disabled while a run is in flight.
export function Composer({
  onSubmit,
  busy,
}: {
  onSubmit: (task: string) => void;
  busy: boolean;
}) {
  const [task, setTask] = useState("");

  const submit = () => {
    const t = task.trim();
    if (!t || busy) return;
    onSubmit(t);
    setTask("");
  };

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
        placeholder={busy ? "A run is in progress…" : "Ask the agency to do something…"}
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
      <button className="btn btn--primary composer__send" type="submit" disabled={busy || !task.trim()}>
        Run
      </button>
    </form>
  );
}
