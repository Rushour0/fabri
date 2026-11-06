import { useEffect, useState, useCallback } from "react";
import { getGitHubAppInfo, listGitHubInstalls, disconnectGitHub, type GitHubInstall } from "../lib/api";
import { connectGitHub } from "../lib/githubConnect";
import IntegrationSection from "./IntegrationSection";

export default function ConnectGitHub({
  locked = false,
  onRequireAuth = () => {},
}: {
  locked?: boolean;
  onRequireAuth?: () => void;
}) {
  const [installs, setInstalls] = useState<GitHubInstall[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<"connected" | "error" | null>(null);
  const [configured, setConfigured] = useState(false);
  const [connecting, setConnecting] = useState(false);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setInstalls(await listGitHubInstalls());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load GitHub installations");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Signed-out visitors see the pitch, not a 401.
    if (locked) return;
    void refetch();
  }, [locked, refetch]);

  useEffect(() => {
    // Display only: is the App configured. The signed URL expires in 10
    // minutes, so the click handler fetches its own fresh one.
    if (locked) return;
    const loadAppInfo = async () => {
      try {
        const info = await getGitHubAppInfo();
        setConfigured(Boolean(info.install_url));
      } catch {
        setConfigured(false);
      }
    };
    void loadAppInfo();
  }, [locked]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const github = params.get("github");
    if (github === "connected") setBanner("connected");
    if (github === "error") setBanner("error");
    if (github !== null) {
      params.delete("github");
      const search = params.toString();
      const url = `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`;
      window.history.replaceState({}, "", url);
    }
  }, []);

  const handleConnect = async () => {
    setError(null);
    setConnecting(true);
    try {
      await connectGitHub(getGitHubAppInfo, (url) => window.location.assign(url));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start GitHub connection");
      setConnecting(false);
    }
  };

  const handleDisconnect = async (installationId: string) => {
    setError(null);
    try {
      await disconnectGitHub(installationId);
      await refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disconnect GitHub installation");
    }
  };

  return (
    <IntegrationSection
      name="GitHub"
      blurb="Point an agency at a repo. It reads the code, does the work on a branch, and comes back with a pull request to review."
      brand="github"
      hue="#9d8cff"
      handoff={["Give it a repo task", "Agency edits a branch", "Pull request opened"]}
      locked={locked}
      onRequireAuth={onRequireAuth}
      connect={
        configured ? (
          <button className="btn" onClick={() => void handleConnect()} disabled={connecting} aria-busy={connecting}>
            {connecting ? "Connecting…" : "Connect GitHub"}
          </button>
        ) : (
          <span className="integration__unavailable">
            GitHub App not configured on this server.
          </span>
        )
      }
    >
      {banner === "connected" && <div role="status">GitHub installation connected</div>}
      {banner === "error" && <div className="error-banner">Could not connect GitHub installation</div>}
      {loading ? (
        <p>Loading GitHub installations…</p>
      ) : error ? (
        <div className="error-banner">{error}</div>
      ) : installs.length === 0 ? (
        <p>No GitHub installations connected.</p>
      ) : (
        <div>
          {installs.map((install) => (
            <div key={install.installation_id}>
              <span>{install.account_login || install.installation_id}</span>{" "}
              <button className="btn" onClick={() => void handleDisconnect(install.installation_id)}>
                Disconnect
              </button>
            </div>
          ))}
        </div>
      )}
    </IntegrationSection>
  );
}
