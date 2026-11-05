# Adding a connector

A connector is one Python module in `src/fabri/tools/connector_catalog/`. The catalog loads it, lists it in
`fabri setups tools connectors`, and expands it into tool-source YAML when someone runs
`fabri setups tools add connector <id>`. Read [adding-tools.md](adding-tools.md) first for what a tool source is and how
approval floors work.

## The module contract

The module name is the connector id. It defines:

| Name | Required | What it is |
|---|---|---|
| `ENTRY` | yes | A `Connector(...)`: id, title, one-line description, allowed source kinds, default token env var, credential provider, remote URL, and optional `spec_file`, `default_allow`, `verify_path`. `verify_path` is the GET that `fabri connect` calls to check a token. |
| `expand(name, *, token_env, workspace, remote, url, all_tools)` | yes | Returns `{"sources": [...], "allow": [...]}`. Build the credential header with `_credential(ENTRY, token_env, workspace)` so `--workspace` produces a `provider:handle` secret ref. |
| `minimum(tool_name, method)` | yes | Returns an approval class, or `None`. Name-keyed floor for sources that keep the `catalog` marker. |
| `HOSTS` | for http, openapi | Tuple of lower-case host names the connector owns. A host matches itself and its subdomains. |
| `READS` | for http, openapi | Tuple of `(METHOD, path regex)` pairs trusted as read. The regex must match the whole path. Everything else on the host is `always_approve`. |

Every expanded source carries two markers, `approval_mode: tighten` and `catalog: <id>`. They are for the reviewer. The
real floor comes from `HOSTS` and `READS`, so it holds when someone edits the YAML. Never let YAML lower a class.

A connector that sends data out of the install (mail, chat, a campaign) must have a write floor. If you are unsure
whether a call is a read, it is not.

## Credentials

Use a `provider:handle` secret ref, never a literal token. `fabri connect <provider>` stores the token, and the
credential store resolves it at call time. The loader needs no code from you beyond `ENTRY.verify_path` and the
`_credential` helper. Do not log, format or raise anything that contains a header value; run any message built near a
token through `redact` from `fabri.tools.mcp_client`.

OAuth sign-in (`oauth` on the entry) and the `identity` hook that derives a handle from a token response land in the
next wave. Until then a connector takes a pasted token.

## Pinned specs

An openapi connector ships a trimmed copy of the vendor's spec under `connector_catalog/specs/` and the script that
makes it, for example `scripts/trim_cakemail_spec.py`. The pattern:

1. Keep the full spec as a fixture in `tests/fixtures/`, named with the version.
2. Write a deterministic trim script that keeps only the curated operations and their `security` blocks.
3. A test compares the packaged spec to the script's output, so a hand edit or a stale copy fails.
4. A second test fails if a curated operation id disappears or a denied family appears.

`docs/connectors/cakemail.md` ends with the bump steps for the Cakemail spec.

## Tests to copy

`tests/test_connector_catalog.py` holds the shared patterns. Copy the shape for a new connector:

- A snapshot test of the entry fields, so a rename is a visible diff (`test_three_entries_match_v0_27_0_snapshot`).
- A floor test per host: a write to your host is `always_approve`, a listed read is not (`test_request_floor_by_host`).
- A loader test that your module's `HOSTS` and `READS` pass the checks (`test_loader_checks_hosts_and_reads`).
- An expansion test that checks the generated YAML, the allow list, and that the header is a secret ref and never a
  token.

Run the file with:

```sh
.venv/bin/python -m pytest tests/test_connector_catalog.py -q
```
