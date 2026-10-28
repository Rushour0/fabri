# Setups API (fabri serve, used by Fabri Studio)

These endpoints let a host manage layered setups (agents, skills, context, data, tools, plays) from one place. They are generic and know nothing about any product. They are backed by `fabri.setups` and `fabri.tools`.

## Configuration

`fabri serve --setups-root DIR [--setups-config FILE]`. The config file is YAML:

```yaml
root: data                 # layer root on disk
layers:                    # how scopes map to directories, in merge order
  - {kind: baseline, path: baseline}
  - {kind: pack,    path: "packs/{pack}"}
  - {kind: org,     path: "companies/{org}"}
  - {kind: project, path: "companies/{org}/projects/{project}"}
global_allow: tools-allow.yaml   # optional global tool allowlist
policy: strict                   # tool registration policy: strict | permissive
```

An org lists its packs in its own `setup.yaml` under `packs:`.

`fabri setups init DIR` writes a starter folder with this config: a baseline
with two workers (`support`, `triage`), shared skills and four helpdesk tools,
a `saas` pack, two companies with their own context and skills, and one
project. Run `fabri studio --setups-config DIR/setups.yaml` to open it.

The config can also carry a `ui:` block that Studio reads from `GET
/setups/ui`, so one Studio build fits different products:

```yaml
ui:
  title: Support setups
  intro: One paragraph under the title.
  labels:                    # any of: agent_type(s), worker(s), company/companies,
    agent_types: Industries  # project(s), global_skills, skills, context, data,
    workers: Roles           # tools, merged, preview
  tabs: [companies, projects, agents, skills, tools, preview, merged]   # order and subset
  default_tab: companies
```

Unknown keys, unknown labels and unknown tabs are refused when the server
starts.

Every route requires the service's existing auth. Writes need the `admin` role. Concretely:

- Reads: a Studio session (when `auth.enabled`), or `Authorization: Bearer $FABRI_SERVICE_TOKEN` (or the admin token). With none of these configured the API is open, like the rest of an un-authed `fabri serve`.
- Writes (anything that is not a GET): `Authorization: Bearer $FABRI_ADMIN_TOKEN`, or a session whose email is listed under `auth.admins` in the service config, or `FABRI_SERVICE_TOKEN` when neither an admin token nor session auth is configured. `/tools/probe` and `/tools/sync` count as writes (they call out and write state). 401 means no valid credentials, 403 means authenticated without the admin role.
- The audit `actor` is the session email, `admin-token`, `service-token`, or `anonymous` when the gate is open.

The same rules cover the rest of `fabri serve` once `auth.enabled` is on: every route except `/health`, `/auth/*` and signed webhooks needs one of the credentials above, and anything that starts or changes work (`POST /runs`, `/fleets`, answer, cancel, install routes) needs admin. `auth.allow_guests: true` brings back anonymous runs. `GET /surfaces/health` (admin) lists each connected surface (Slack, GitHub, Linear) with the env vars it is missing, by name only, its install count, and whether it has a way to post results back. `fabri installs list` and `fabri installs remove <surface> <id>` read and edit the same install database offline (`--home-root`, else `$FABRI_INSTALL_DB`, else `$FABRI_HOME/serve`), without printing tokens. Linear installs store the refresh token Linear returns; the server swaps in a new access token when the stored one is within 5 minutes of its 24 hour expiry. Installs made before 0.26.0 have no refresh token, so reconnect them once.

Invite-only service config, passed with `fabri serve --auth-config FILE` (or `FABRI_AUTH_CONFIG=FILE`) so it stays out of run configs:

```yaml
auth:
  enabled: true
  signup: false                 # /auth/signup answers 403
  admins: [you@example.com]
  allowed_emails: [you@example.com, teammate@example.com]
  db_path: /data/fabri/auth.db
```

- `allowed_emails` (or `FABRI_AUTH_ALLOWED_EMAILS`, comma list): signup and login for any other email get the same answers as a taken email or a wrong password, after the same password-hash work. A session whose email drops off the list stops working.
- `FABRI_AUTH_ADMINS`, `FABRI_AUTH_SIGNUP` and `FABRI_AUTH_DB_PATH` override the file. `FABRI_AUTH_SECRET` signs sessions.
- Users without signup: `fabri auth add-user EMAIL` and `fabri auth set-password EMAIL`, with the server's `--auth-config` (or `FABRI_AUTH_CONFIG`). The password comes from a prompt, or one line on stdin.
- `GET /auth/check` answers 204 when the caller could read and 401 otherwise, for a proxy's `forward_auth` in front of non-fabri paths. `GET /auth/config` tells Studio whether signup and guests are on.

## Endpoints

| Method and path | Body | Returns |
|---|---|---|
| `GET /setups/scopes` | | `{baseline: {...}, packs: [name], orgs: [{name, packs, projects: [name]}]}` |
| `POST /setups/scopes` | `{kind: pack\|org\|project, name, org?}` | the created scope |
| `GET /setups/items?scope=S&kind=K` | | `[{name, kind, source_layer, size, updated_at, enabled}]` |
| `GET /setups/item?scope=S&kind=K&name=N` | | `{name, kind, files: {path: text}, meta, lint: [..]}` |
| `GET /setups/item/history?scope=S&kind=skills&name=N` | | `{name, kind, revision, history: [{revision, sha, at, actor, deleted}]}` (last 50) |
| `PUT /setups/item?scope=S&kind=K&name=N` | `{files: {path: text}}` | `{ok, lint: [..]}`, written atomically, live on the next resolve |
| `DELETE /setups/item?scope=S&kind=K&name=N` | | `204` |
| `POST /setups/item/disable` | `{scope, kind, name, disabled: bool}` | `{ok}` |
| `GET /setups/resolved?org=&project=&type=` | | the merged setup, each item tagged with its source layer, the resolved tool set, and the route options |
| `GET /setups/preview?org=&project=&type=&task=&skill_top_k=&tool_top_k=` | | for one agent type and task: always-loaded skills with sizes, on-demand skills ranked with scores and whether each gets a full index line, tools ranked with scores and whether the model is shown them, prompt size against the budgets, and the ranking method (`embeddings` or `words`) |
| `GET /setups/ui` | | the config's `ui:` block, or `{}` |
| `GET /runs/tool-usage?limit=&agency=` | | per-tool calls, failures, refusals, seconds and run count summed over the newest `limit` runs (default 200) that reported tool usage |
| `POST /setups/import` | `{scope, format: skill-md\|fabri, files: {path: text}, dry_run}` | `{created, updated, skipped, invalid}` |
| `GET /setups/lint?scope=S` | | `[{severity, kind, name, message}]` |
| `GET /tools/catalog?org=&project=&type=` | | catalog entries: name, alias, title, description, approval, source, endpoint, params, options |
| `POST /tools/register` | `{scope, decl}` | `RegisteredTool`, or 400 with the policy reasons |
| `POST /tools/unregister` | `{scope, name}` | `{ok}` |
| `POST /tools/enable` | `{scope, name, enabled}` | `{ok}` |
| `POST /tools/sync` | `{scope}` | `{added, removed, changed}` |
| `POST /tools/probe` | `{scope, ops?}` | `{probed, skipped}` (safe GETs only) |
| `GET /tools/sources?scope=S` | | `{scope, entries: [{name, file, origin, kinds, sources, enabled, allow, tools, approvals}], errors}` |
| `POST /tools/sources` | `{scope, source: openapi\|mcp\|connector, ..., dry_run?, replace?}` | `{name, file, tool_count, approvals, tools, hidden, errors, warnings, lint, written, yaml}` |
| `POST /tools/sources/enable` | `{scope, name, enabled}` | `{ok, changed}` |
| `DELETE /tools/sources?scope=S&name=N` | | `{ok, removed: [path]}` |
| `GET /tools/connectors` | | `[{name, title, summary, kinds, env_var, store_provider, remote_option}]` |
| `GET /setups/agent-types` | | `[{type, title, route?, global_skills: [name], used_by: [org]}]` |
| `GET /setups/agent-types/{type}` | | `{type, title, prompt, global_skills: [name]}` |
| `PUT /setups/agent-types/{type}` | `{prompt?, global_skills?: [name]}` | the same object after the write |
| `GET /setups/orgs/{org}/agent-types` | | `{agents: [type], available: [type], explicit: bool}` |
| `PUT /setups/orgs/{org}/agent-types` | `{agents: [type]}` | `{ok, agents}` |

Details that the table leaves out:

- `GET /setups/item` on a skill also returns `revision`. Every skill write or delete through the API bumps the revision when the files changed and records the audit actor in the skill history.
- `GET /setups/resolved?type=T` marks each skill with `load_mode` (`always` or `on_demand`). `system_prompt` lists on-demand skills by name and description only; `config["setup"]["on_demand_skills"]` from `build_agent_config` names them, and the agent gets a `load_skill` tool.
- `GET /setups/item` reads the layer named by `scope` only. An item listed with a `source_layer` other than the scope is opened with `scope=<that source_layer>`. Layer names in every response are scope strings.
- `PUT /setups/item` replaces the whole item. Files not in the body are removed from a directory item (`skills/<name>/`). Allowed file names: agents `agent.yaml`, `prompt.md`, `prompt.append.md`; skills `skill.yaml`, `prompt.md` and `references/<name>.md|txt|json|yaml|yml|csv`; plays `play.yaml`; context `<name>.md`; data `<name>`; tools `tools.yaml`; setup `setup.yaml`.
- Kind `setup` is the layer's `setup.yaml` (name must be `setup.yaml`, files `{"setup.yaml": text}`). `GET` returns 404 while the layer has none, `PUT` validates the YAML (400 with the parse error), `DELETE` removes it.
- Kind `tools` is `tools/tools.yaml` only (name `tools.yaml`). Single tools go through `/tools/register`. A `tools.yaml` that holds a literal credential in `headers` or `env` is refused on `PUT` (use `secret_ref` or `env`) and redacted on `GET`.
- `GET /setups/items` lists what the scope's view contains (its own layer plus inherited layers), each tagged with `source_layer`. Items disabled in the scope's own layer appear with `enabled: false`.
- `GET /setups/lint` returns `{severity, kind, name, message, layer}` for every layer in the scope's view.
- `GET /setups/resolved` adds `resolved_tools`, `tool_errors`, `scope` and, when `type` is given, `skill_order` to the merged setup. Layer paths are not returned. `org` is needed for `project`; without either, the view is baseline.
- `GET /tools/catalog` entries carry `endpoint: {method, path, kind}`, `layer`, `origin`, `examples` and `probed_at` besides the listed fields.
- `POST /setups/scopes` returns 201, or 409 when the scope exists. A project needs its org to exist.
- Import `files` are paths relative to an upload root, such as `my-skill/SKILL.md`. A bare `SKILL.md` becomes the skill `imported-skill`. Absolute paths, `..` and hidden segments are rejected. The response also carries `warnings` and `dry_run`.
- `/tools/sources` manages `<layer>/tools/sources/<name>.yaml`, one plain YAML file per added source (see [adding-tools.md](adding-tools.md)). `POST` body fields by `source`:
  - `openapi`: `spec_url` or `spec_text` (the spec as text; server paths are not accepted), `name?`, `include?`, `exclude?`, `methods?`, `allow_delete?`, `base_url?`, `snapshot?`, `strip_prefixes?`.
  - `mcp`: `name`, `command` (string or list) or `url`, `env?` (`["NAME", "CHILD=PARENT"]` or a mapping, stdio only), `include?`, `connect?` (default true: start or call the server to list its tools).
  - `connector`: `connector` (`github`, `linear`, `slack`), `name?`, `token_env?`, `workspace?`, `remote?`, `connect?` (default false).
  - All: `approval?` (a floor: `read`, `write` or `always_approve`), `auth?: {env | secret_ref, header?, prefix?}`, `headers?: {Name: ENV_VAR}`, `dry_run?`, `replace?`. Credentials are env var names or secret refs only; anything else is 400.
- `POST /tools/sources` checks the source builds under the server's policy before writing. With `dry_run: true` it always returns 200 with `written: false` and any `errors`; otherwise a source that does not build is 400 with `reasons`, and a name that exists is 409 unless `replace: true`. `yaml` is the file text that was (or would be) written. `lint` holds findings for this source after the write; a credential that is not set on the server is a warning.
- `GET /tools/sources` builds the scope's tools to count them, so MCP servers in the scope are started or called. `tools` and `approvals` count tools that source builds in this layer. Entries from `tools/tools.yaml` and `tools/registered/` are listed too; enable and delete work on `tools/sources` files and registered tools only.
- Default approval classes: GET is `read`, DELETE and operations that send messages or move money are `always_approve`, other methods are `write`; MCP tools are `write`, or `always_approve` when destructive or sending. A policy's `approval_floor` raises classes. Lint warns when a YAML sets a class looser than the default.
- Errors are `{error}` with 400 (invalid input, validation or policy), 401, 403, 404, 405, 409, 413 or 415. Policy and registration refusals add `reasons: [text]`.

Scope string: `baseline` | `pack:<name>` | `org:<org>` | `org:<org>/project:<project>`. Kind: `agents` | `skills` | `context` | `data` | `tools` | `plays`.

## Rules

- Path segments are validated. Writes never escape the layer root.
- Secrets never appear in responses. Tool auth is shown as a secret reference name only.
- Every write appends to `<layer>/.fabri-audit.jsonl` with actor, action, scope, kind, name, content hash and timestamp.

## Agent types and companies

An agent type is a kind of agent (`chat`, `coder`) defined once in the baseline layer. Companies choose which types they use.

- Global skills belong to an agent type. A global skill is a baseline skill whose `skill.yaml` lists the type under `types`. `PUT /setups/agent-types/{type}` with `global_skills` adds the type to the listed baseline skills and removes it from baseline skills that were bound to the type and are no longer listed. Removing the last type from a skill is refused (400), since `types: []` would load it for every type; delete or disable the skill instead. The baseline skill must already exist.
- Local skills belong to a project. Use the item endpoints with scope `org:<org>/project:<p>` and kind `skills`.
- A company picks its types with `agents: [...]` in `companies/<org>/setup.yaml`. A type not listed is disabled for that company. An org with no `agents` key uses every type. `PUT /setups/orgs/{org}/agent-types` writes the list into `setup.yaml` and, for each type that exists now and is not listed, also writes `agent_types: {<type>: false}`, which `fabri.setups` already honors. The endpoint keeps other keys such as `packs`. Types added to the baseline later are not disabled by `agent_types` until the list is saved again; the API still refuses them for that org (`GET /setups/resolved` and `GET /tools/catalog` with `type=` return 404).
- `GET /setups/orgs/{org}/agent-types` and `kind=setup` on `/setups/item` read and write the same file. Edit either.
- `PUT /setups/agent-types/{type}` sets the baseline `agents/<type>/prompt.md`. The type must already exist (create it with kind `agents`). Validation (budgets, skill manifests) runs for every file before any file is written.

Resolution rule. An agent of type T for (org, project) runs only if the org enabled T. It loads, in this order: T's global skills, then the project's local skills, then org and pack skills, then other baseline skills. `GET /setups/resolved?org=&project=&type=T` returns that order as `skill_order: [{name, source_layer, group}]` with groups `global`, `project`, `org-or-pack`, `baseline`. `fabri.setups` itself still orders `skills_for(T)` by layer (baseline first). A host that wants the order above must follow `skill_order`.

Not supported: skills stored under `agents/<type>/skills/<name>/`. `fabri.setups` does not read that directory, so global skills are expressed with `types`.
