# Composio connector (experimental)

Adds Composio's hosted MCP server as one `mcp` source, so an agent can call the tools of the apps connected in your
Composio account. It is experimental and the add command writes it disabled.

```sh
fabri setups tools add connector composio --scope org:acme \
  --url https://your-composio-mcp-url --include GMAIL_SEND_EMAIL GITHUB_LIST_ISSUES --workspace main
fabri setups tools enable composio --scope org:acme    # nothing builds until you do this
```

## What you must give

- `--url`: the MCP server URL from your Composio account. https only.
- `--include`: the exact tool names to expose. No wildcards. The connector exposes many tools, so it will not guess.

## What it writes

- A source with `enabled: false`, `header x-api-key` (no prefix) from `COMPOSIO_API_KEY`, or from a secret ref
  `composio:<workspace>` with `--workspace`.
- `allow` lists `mcp_composio_<name>` for each included tool.
- `approval_mode: tighten` and `catalog: composio`.

## Approval

Every Composio tool is `always_approve`, including ones that look like reads. The module's `READ_OK` list is empty in
this release, and the `catalog` marker keeps the floor on whatever the YAML says. Loosening one tool later is a
reviewed code change to that list, not a YAML edit. The module sets no `HOSTS`, because you choose the server URL, so the floor comes from the `catalog` marker and
the tool name only. If someone removes `catalog:` from the YAML, the floor is gone. With no approver configured, calls are refused.

## What is not confirmed

- UNVERIFIED: the auth header (`x-api-key`) and the exact URL shape of Composio's MCP endpoint.
- UNVERIFIED: how Composio handles data stored for the connected apps.
- Composio has no Cakemail toolkit (the plan checked this), so use the Cakemail connector for Cakemail.
- There is no Composio SDK in fabri. Only plain MCP over HTTP is supported.
