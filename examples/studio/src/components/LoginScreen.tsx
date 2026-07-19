import { useState, type CSSProperties, type FormEvent } from "react";
import { login, signup } from "../lib/auth";

export function LoginScreen({ onAuthed }: { onAuthed: (email: string) => void }) {
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await (mode === "login" ? login(email, password) : signup(email, password));
      onAuthed(email);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Something went wrong. Please try again.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <main
      style={{ minHeight: "100%", display: "grid", placeItems: "center", padding: 24 }}
    >
      <form
        onSubmit={submit}
        style={{
          width: "min(100%, 360px)",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: 12,
          boxShadow: "var(--shadow-md)",
          padding: 24,
        }}
      >
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", gap: 10, marginBottom: 20, fontSize: 16, fontWeight: 600 }}>
          <span className="header__logo" aria-hidden />
          <span>Fabri Studio</span>
        </div>
        <div className="tabs" aria-label="Authentication mode" style={{ marginBottom: 18 }}>
          <button
            className={"tab" + (mode === "login" ? " tab--on" : "")}
            style={{ flex: 1 }}
            type="button"
            onClick={() => setMode("login")}
            aria-pressed={mode === "login"}
          >
            Log in
          </button>
          <button
            className={"tab" + (mode === "signup" ? " tab--on" : "")}
            style={{ flex: 1 }}
            type="button"
            onClick={() => setMode("signup")}
            aria-pressed={mode === "signup"}
          >
            Sign up
          </button>
        </div>
        <label style={fieldStyle} htmlFor="auth-email">
          Email
          <input
            id="auth-email"
            className="composer__input"
            style={{ width: "100%", padding: "9px 10px" }}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </label>
        <label style={fieldStyle} htmlFor="auth-password">
          Password
          <input
            id="auth-password"
            className="composer__input"
            style={{ width: "100%", padding: "9px 10px" }}
            type="password"
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </label>
        {error && <div style={{ marginTop: 13, color: "var(--err)", fontSize: 12.5 }} role="alert">{error}</div>}
        <button className="btn btn--primary" style={{ width: "100%", marginTop: 18 }} type="submit" disabled={submitting}>
          {submitting ? "Please wait…" : mode === "login" ? "Log in" : "Create account"}
        </button>
      </form>
    </main>
  );
}

const fieldStyle: CSSProperties = {
  display: "grid",
  gap: 5,
  marginTop: 13,
  color: "var(--text-dim)",
  fontSize: 12,
  fontWeight: 500,
};
