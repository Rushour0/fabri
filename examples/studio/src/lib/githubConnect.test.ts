import { describe, it, expect, vi } from "vitest";
import { connectGitHub } from "./githubConnect";

describe("connectGitHub", () => {
  it("fetches a fresh URL on click and navigates to that one, not an earlier one", async () => {
    const fetchInfo = vi
      .fn()
      .mockResolvedValueOnce({ install_url: "https://github.com/apps/x/installations/new?state=mount" })
      .mockResolvedValueOnce({ install_url: "https://github.com/apps/x/installations/new?state=fresh" });
    const navigate = vi.fn();
    await fetchInfo(); // mount-time fetch
    await connectGitHub(fetchInfo, navigate);
    expect(fetchInfo).toHaveBeenCalledTimes(2);
    expect(navigate).toHaveBeenCalledTimes(1);
    expect(navigate).toHaveBeenCalledWith("https://github.com/apps/x/installations/new?state=fresh");
  });
  it("rejects and does not navigate when the fetch fails", async () => {
    const navigate = vi.fn();
    await expect(connectGitHub(() => Promise.reject(new Error("boom")), navigate)).rejects.toThrow("boom");
    expect(navigate).not.toHaveBeenCalled();
  });
  it("rejects when no URL comes back", async () => {
    const navigate = vi.fn();
    await expect(connectGitHub(async () => ({ install_url: null }), navigate)).rejects.toThrow();
    expect(navigate).not.toHaveBeenCalled();
  });
});
