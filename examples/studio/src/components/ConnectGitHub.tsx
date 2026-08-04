import { useEffect, useState, useCallback } from "react";
import { getGitHubAppInfo, listGitHubInstalls, disconnectGitHub, type GitHubInstall } from "../lib/api";
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
  const [installUrl, setInstallUrl] = useState<string | null>(null);

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
    // The install URL comes from an authenticated endpoint, so signed-out
    // visitors never have one — the section shows the sign-in prompt instead.
    if (locked) return;
    const loadAppInfo = async () => {
      try {
        const info = await getGitHubAppInfo();
        setInstallUrl(info.install_url);
      } catch {
        setInstallUrl(null);
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
        installUrl ? (
          <a href={installUrl}>Connect GitHub</a>
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
