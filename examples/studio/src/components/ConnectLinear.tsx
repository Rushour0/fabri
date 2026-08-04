import { useEffect, useState, useCallback } from "react";
import { SquareKanban } from "lucide-react";
import { listLinearInstalls, disconnectLinear, type LinearInstall } from "../lib/api";
import IntegrationSection from "./IntegrationSection";

export default function ConnectLinear({
  locked = false,
  onRequireAuth = () => {},
}: {
  locked?: boolean;
  onRequireAuth?: () => void;
}) {
  const [installs, setInstalls] = useState<LinearInstall[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [banner, setBanner] = useState<"connected" | "error" | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setInstalls(await listLinearInstalls());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load Linear workspaces");
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
    const linear = params.get("linear");
    if (linear === "connected") setBanner("connected");
    if (linear === "error") setBanner("error");
    if (linear !== null) {
      params.delete("linear");
      const search = params.toString();
      const url = `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`;
      window.history.replaceState({}, "", url);
    }
  }, []);

  const handleDisconnect = async (workspaceId: string) => {
    setError(null);
    try {
      await disconnectLinear(workspaceId);
      await refetch();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not disconnect Linear workspace");
    }
  };

  return (
    <IntegrationSection
      name="Linear"
      blurb="Hand an agency a ticket. It reads the issue, works it, and reports back on the same issue where your team already looks."
      icon={SquareKanban}
      hue="#5e6ad2"
      handoff={["Assign an issue", "Agency works the ticket", "Issue updated in place"]}
      locked={locked}
      onRequireAuth={onRequireAuth}
      connect={<a href="/linear/install">Connect Linear</a>}
    >
      {banner === "connected" && <div role="status">Linear workspace connected</div>}
      {banner === "error" && <div className="error-banner">Could not connect Linear workspace</div>}
      {loading ? (
        <p>Loading Linear workspaces…</p>
      ) : error ? (
        <div className="error-banner">{error}</div>
      ) : installs.length === 0 ? (
        <p>No Linear workspaces connected.</p>
      ) : (
        <div>
          {installs.map((install) => (
            <div key={install.workspace_id}>
              <span>{install.workspace_name || install.workspace_id}</span>{" "}
              <button className="btn" onClick={() => void handleDisconnect(install.workspace_id)}>
                Disconnect
              </button>
            </div>
          ))}
        </div>
      )}
    </IntegrationSection>
  );
}
