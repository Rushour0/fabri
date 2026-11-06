/**
 * The signed install URL from /github/app-info expires after 10 minutes, so it
 * must never be reused from page load. Fetch a fresh one on click and go.
 */
export async function connectGitHub(
  fetchInfo: () => Promise<{ install_url: string | null }>,
  navigate: (url: string) => void,
): Promise<void> {
  const info = await fetchInfo();
  if (!info.install_url) throw new Error("GitHub App not configured on this server");
  navigate(info.install_url);
}
