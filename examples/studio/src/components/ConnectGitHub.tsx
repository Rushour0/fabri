import { useEffect, useState, useCallback } from "react";
import { getGitHubAppInfo, listGitHubInstalls, disconnectGitHub, type GitHubInstall } from "../lib/api";

export default function ConnectGitHub() {
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
    void refetch();
  }, [refetch]);

  useEffect(() => {
    const loadAppInfo = async () => {
      try {
        const info = await getGitHubAppInfo();
        setInstallUrl(info.install_url);
      } catch {
        setInstallUrl(null);
      }
    };
    void loadAppInfo();
  }, []);

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
    <section>
      <h2>GitHub</h2>
      {banner === "connected" && <div role="status">GitHub installation connected</div>}
      {banner === "error" && <div className="error-banner">Could not connect GitHub installation</div>}
      {installUrl && (
        <p>
          <a href={installUrl}>Connect GitHub</a>
        </p>
      )}
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
    </section>
  );
}
