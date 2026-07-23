# GitHub through MCP

> **Status:** config verified against fabri's local MCP client schema; **not yet
> live-verified** against the GitHub MCP server or a real GitHub PAT.

GitHub is configuration, not a new fabri tool. fabri starts a configured MCP
server when it builds an agent, discovers that server's tools, and wraps each
one as a normal fabri tool.

## What you get

With the server name `github`, every discovered remote tool is exposed as
`mcp_github_<remote_tool>`. The exact suffixes come from the server's
`tools/list` response, so they are deliberately not enumerated here without a
live verification.

The connection is made at agent-build time. If the server cannot start,
initialize, or list its tools, fabri logs a warning, skips that server, and
continues building the agent without the GitHub tools. If the agent config sets
`tools.enabled`, it must include each discovered `mcp_github_<remote_tool>` name
explicitly; otherwise leave `tools.enabled` unset.

## Configure the official GitHub MCP server

Export the token in the environment that launches fabri:

```sh
export GITHUB_PERSONAL_ACCESS_TOKEN='github_pat_...'
```

Then add this block to the agent config:

```yaml
tools:
  mcp_servers:
    - name: github
      command:
        - docker
        - run
        - --rm
        - -i
        - -e
        - GITHUB_PERSONAL_ACCESS_TOKEN
        - ghcr.io/github/github-mcp-server
```

This chooses GitHub's official `github-mcp-server` container rather than a
third-party bridge. The package is the container image
`ghcr.io/github/github-mcp-server`; `docker` is the executable, and the entire
invocation is one `command` argv list because fabri has no separate `args` key.
`-i` keeps stdin open for MCP's stdio transport. Docker's
`-e GITHUB_PERSONAL_ACCESS_TOKEN` forwards the variable inherited by the
`docker` process into the container. The official server expects that exact
PAT variable name.

fabri also accepts an `env` map for stdio servers and merges it over the
launcher's environment. Do not put a raw PAT in checked-in YAML, and do not use
`${GITHUB_PERSONAL_ACCESS_TOKEN}` there: `yaml.safe_load` does not expand shell
variables.

## Credential handles

`fabri.tools.secret_refs` resolves a handle such as `github:acme` from
`FABRI_CRED_GITHUB_ACME`. The MCP config parser does **not** call that resolver,
so this is not valid:

```yaml
env:
  GITHUB_PERSONAL_ACCESS_TOKEN: github:acme
```

For a handle-backed local setup, have the launching shell or secret manager set
the server's expected variable from the backing variable before fabri starts:

```sh
export FABRI_CRED_GITHUB_ACME='github_pat_...'
export GITHUB_PERSONAL_ACCESS_TOKEN="$FABRI_CRED_GITHUB_ACME"
```

The YAML above then works unchanged because the MCP subprocess inherits the
launcher environment.

## Least-privilege PAT

Prefer a fine-grained PAT, restrict repository access to only the repositories
this agent needs, and give it an expiry. For a read-and-propose-PR flow, grant:

- **Metadata: read-only** (included automatically by GitHub).
- **Contents: read and write** so the server can read files and create commits
  on a proposal branch.
- **Pull requests: read and write** so it can inspect and open or update the PR.
- **Issues: read-only** only if the work starts from issue context; omit it
  otherwise.

Do not grant Administration, Actions, Workflows, Webhooks, Secrets, or
organization-wide access unless a separately reviewed workflow needs them.
Classic PAT scopes such as `repo` are broader; use one only when fine-grained
tokens cannot support the target repository or operation.

## Propose, don't apply

Treat GitHub writes as proposals:

1. Work on a new, task-specific branch.
2. Push only that branch; never push directly to the default or a protected
   branch.
3. Open or update a PR with the diff and verification evidence.
4. Leave approval and merge to a human and to repository branch protection.

The PAT and repository rules are the hard boundary. A prompt instruction is
helpful, but it is not a substitute for selected-repository access, protected
branches, required reviews, and a token that cannot administer the repository.

## Startup and compatibility notes

fabri uses stdio JSON-RPC with newline-delimited messages. On startup it spawns
the command, sends `initialize`, calls `tools/list`, and registers the returned
tools. Server stdout is therefore a protocol channel; diagnostics must go to
stderr.

This page does not establish that the current GitHub container and fabri's MCP
protocol version interoperate in practice. Container pull/startup, tool
discovery, a real PAT, GitHub permissions, and an end-to-end branch/PR flow all
remain to be live-verified.

## Appendix: schema evidence

These are the parsing lines in `src/fabri/tools/mcp_client.py:273-291`:

```python
name = server_cfg.get("name") or "mcp"
command = server_cfg.get("command")
url = server_cfg.get("url")
if command and url:
    raise ValueError(
        f"mcp server {name!r}: provide either 'command' (stdio) or "
        f"'url' (http), not both"
    )
if url:
    client = MCPHttpClient(
        url=url, headers=server_cfg.get("headers"), name=name,
        timeout_s=float(server_cfg.get("timeout_s", 30.0)),
    )
elif command:
    env = server_cfg.get("env")
    client = MCPStdioClient(command=command, env=env, name=name)
else:
    raise ValueError(
        f"mcp server {name!r}: must set 'command' (stdio) or 'url' (http)"
    )
```

The server-config keys read by that parser are therefore:

- Common: `name`.
- Stdio: `command`, `env`.
- HTTP: `url`, `headers`, `timeout_s`.

`command` and `url` are mutually exclusive, and one is required. There is no
`args` key.

Environment inheritance is defined at
`src/fabri/tools/mcp_client.py:87-96`:

```python
# Popen's `env=` REPLACES the whole environment; passing only the
# configured overrides would strip PATH/FABRI_HOME and break the server.
# Merge onto the inherited environment instead.
child_env = {**os.environ, **self.env} if self.env else None
self.proc = subprocess.Popen(
    self.command,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env=child_env,
```

Startup, discovery, and tool naming are visible at
`src/fabri/tools/mcp_client.py:293-300`:

```python
client.start()
client.initialize()
remote_tools = client.list_tools()

pairs = []
for spec in remote_tools:
    remote_name = spec.get("name", "unknown")
    fabri_name = f"mcp_{_sanitize_name(name)}_{_sanitize_name(remote_name)}"
```
