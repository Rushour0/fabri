import { describe, it, expect } from "vitest";
import { LEGACY_AUTH_CONFIG, mustSignIn } from "./auth";

describe("mustSignIn", () => {
  const inviteOnly = { signup: false, guests: false };

  it("forces the login for a signed-out visitor on an invite-only server", () => {
    expect(mustSignIn("anon", inviteOnly)).toBe(true);
  });

  it("lets signed-in users, auth-off servers and guest-mode servers through", () => {
    expect(mustSignIn({ email: "a@b.test" }, inviteOnly)).toBe(false);
    expect(mustSignIn("disabled", inviteOnly)).toBe(false);
    expect(mustSignIn("loading", inviteOnly)).toBe(false);
    expect(mustSignIn("anon", LEGACY_AUTH_CONFIG)).toBe(false);
  });
});
