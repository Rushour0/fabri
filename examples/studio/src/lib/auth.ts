export type AuthState = "loading" | "disabled" | "anon" | { email: string };

function hasEmail(body: unknown): body is { email: string } {
  return (
    typeof body === "object" &&
    body !== null &&
    "email" in body &&
    typeof (body as { email: unknown }).email === "string"
  );
}

export async function getMe(): Promise<AuthState> {
  try {
    const res = await fetch("/auth/me", { credentials: "same-origin" });
    if (res.status === 401) return "anon";
    if (!res.ok) return "disabled";

    const body: unknown = await res.json();
    return hasEmail(body) ? { email: body.email } : "disabled";
  } catch {
    // Auth-off Studio serves the SPA's HTML fallback at this path; it cannot be
    // parsed as JSON. Treat any unavailable or non-JSON auth endpoint as off.
    return "disabled";
  }
}

async function submitAuth(path: "/auth/login" | "/auth/signup", email: string, password: string): Promise<void> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ email, password }),
  });
  if (res.ok) return;

  const messages: Record<number, string> = {
    400: "Enter a valid email and password",
    401: "Wrong email or password",
  };
  throw new Error(messages[res.status] ?? "Something went wrong. Please try again.");
}

export function login(email: string, password: string): Promise<void> {
  return submitAuth("/auth/login", email, password);
}

export function signup(email: string, password: string): Promise<void> {
  return submitAuth("/auth/signup", email, password);
}

export async function logout(): Promise<void> {
  await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
}
