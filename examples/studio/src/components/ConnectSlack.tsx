import { useEffect, useState, useCallback } from "react";
import { listSlackInstalls, disconnectSlack, type SlackInstall } from "../lib/api";
import IntegrationSection from "./IntegrationSection";

export default function ConnectSlack({
  locked = false,
  onRequireAuth = () => {},
}: {
  locked?: boolean;
  onRequireAuth?: () => void;
}) {
  const [installs, setInstalls] = useState<SlackInstall[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<"connected" | "error" | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setInstalls(await listSlackInstalls());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load Slack workspaces");
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
    const params = new URLSearchParams(window.location.search);
    const slack = params.get("slack");
    if (slack === "connected") setBanner("connected");
    if (slack === "error") setBanner("error");
    if (slack !== null) {
      params.delete("slack");
      const search = params.toString();
      const url = `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`;
      window.history.replaceState({}, "", url);
    }
  }, []);

  const handleDisconnect = async (teamId: string) => {
    setError(null);
    try {
      await disconnectSlack(teamId);
      await refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disconnect Slack workspace");
    }
  };

  return (
    <IntegrationSection
      name="Slack"
      blurb="Run an agency from a channel. When an agent needs a decision it can't make, it asks in the thread and waits for you."
      brand="slack"
      hue="#36c5f0"
      handoff={["Message the bot", "Agency runs, cost shown", "Answer lands in thread"]}
      locked={locked}
      onRequireAuth={onRequireAuth}
      connect={<a href="/slack/install">Connect Slack</a>}
    >
      {banner === "connected" && <div role="status">Workspace connected</div>}
      {banner === "error" && <div className="error-banner">Could not connect workspace</div>}
      {loading ? (
        <p>Loading Slack workspaces…</p>
      ) : error ? (
        <div className="error-banner">{error}</div>
      ) : installs.length === 0 ? (
        <p>No Slack workspaces connected.</p>
      ) : (
        <div>
          {installs.map((install) => (
            <div key={install.team_id}>
              <span>{install.team_name || install.team_id}</span>{" "}
              <button className="btn" onClick={() => void handleDisconnect(install.team_id)}>
                Disconnect
              </button>
            </div>
          ))}
        </div>
      )}
    </IntegrationSection>
  );
}
