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

Every route requires the service's existing auth. Writes need the `admin` role. Concretely:

- Reads: a Studio session (when `auth.enabled`), or `Authorization: Bearer $FABRI_SERVICE_TOKEN` (or the admin token). With none of these configured the API is open, like the rest of an un-authed `fabri serve`.
- Writes (anything that is not a GET): `Authorization: Bearer $FABRI_ADMIN_TOKEN`, or a session whose email is listed under `auth.admins` in the service config, or `FABRI_SERVICE_TOKEN` when neither an admin token nor session auth is configured. `/tools/probe` and `/tools/sync` count as writes (they call out and write state). 401 means no valid credentials, 403 means authenticated without the admin role.
- The audit `actor` is the session email, `admin-token`, `service-token`, or `anonymous` when the gate is open.

## Endpoints

| Method and path | Body | Returns |
|---|---|---|
| `GET /setups/scopes` | | `{baseline: {...}, packs: [name], orgs: [{name, packs, projects: [name]}]}` |
| `POST /setups/scopes` | `{kind: pack\|org\|project, name, org?}` | the created scope |
| `GET /setups/items?scope=S&kind=K` | | `[{name, kind, source_layer, size, updated_at, enabled}]` |
| `GET /setups/item?scope=S&kind=K&name=N` | | `{name, kind, files: {path: text}, meta, lint: [..]}` |
| `PUT /setups/item?scope=S&kind=K&name=N` | `{files: {path: text}}` | `{ok, lint: [..]}`, written atomically, live on the next resolve |
| `DELETE /setups/item?scope=S&kind=K&name=N` | | `204` |
| `POST /setups/item/disable` | `{scope, kind, name, disabled: bool}` | `{ok}` |
| `GET /setups/resolved?org=&project=&type=` | | the merged setup, each item tagged with its source layer, the resolved tool set, and the route options |
| `POST /setups/import` | `{scope, format: skill-md\|fabri, files: {path: text}, dry_run}` | `{created, updated, skipped, invalid}` |
| `GET /setups/lint?scope=S` | | `[{severity, kind, name, message}]` |
| `GET /tools/catalog?org=&project=&type=` | | catalog entries: name, alias, title, description, approval, source, endpoint, params, options |
| `POST /tools/register` | `{scope, decl}` | `RegisteredTool`, or 400 with the policy reasons |
| `POST /tools/unregister` | `{scope, name}` | `{ok}` |
| `POST /tools/enable` | `{scope, name, enabled}` | `{ok}` |
| `POST /tools/sync` | `{scope}` | `{added, removed, changed}` |
| `POST /tools/probe` | `{scope, ops?}` | `{probed, skipped}` (safe GETs only) |
| `GET /setups/agent-types` | | `[{type, title, route?, global_skills: [name], used_by: [org]}]` |
| `GET /setups/agent-types/{type}` | | `{type, title, prompt, global_skills: [name]}` |
| `PUT /setups/agent-types/{type}` | `{prompt?, global_skills?: [name]}` | the same object after the write |
| `GET /setups/orgs/{org}/agent-types` | | `{agents: [type], available: [type], explicit: bool}` |
| `PUT /setups/orgs/{org}/agent-types` | `{agents: [type]}` | `{ok, agents}` |

Details that the table leaves out:

- `GET /setups/item` reads the layer named by `scope` only. An item listed with a `source_layer` other than the scope is opened with `scope=<that source_layer>`. Layer names in every response are scope strings.
- `PUT /setups/item` replaces the whole item. Files not in the body are removed from a directory item (`skills/<name>/`). Allowed file names: agents `agent.yaml`, `prompt.md`, `prompt.append.md`; skills `skill.yaml`, `prompt.md`; plays `play.yaml`; context `<name>.md`; data `<name>`; tools `tools.yaml`; setup `setup.yaml`.
- Kind `setup` is the layer's `setup.yaml` (name must be `setup.yaml`, files `{"setup.yaml": text}`). `GET` returns 404 while the layer has none, `PUT` validates the YAML (400 with the parse error), `DELETE` removes it.
- Kind `tools` is `tools/tools.yaml` only (name `tools.yaml`). Single tools go through `/tools/register`. A `tools.yaml` that holds a literal credential in `headers` or `env` is refused on `PUT` (use `secret_ref` or `env`) and redacted on `GET`.
- `GET /setups/items` lists what the scope's view contains (its own layer plus inherited layers), each tagged with `source_layer`. Items disabled in the scope's own layer appear with `enabled: false`.
- `GET /setups/lint` returns `{severity, kind, name, message, layer}` for every layer in the scope's view.
- `GET /setups/resolved` adds `resolved_tools`, `tool_errors`, `scope` and, when `type` is given, `skill_order` to the merged setup. Layer paths are not returned. `org` is needed for `project`; without either, the view is baseline.
- `GET /tools/catalog` entries carry `endpoint: {method, path, kind}`, `layer`, `origin`, `examples` and `probed_at` besides the listed fields.
- `POST /setups/scopes` returns 201, or 409 when the scope exists. A project needs its org to exist.
- Import `files` are paths relative to an upload root, such as `my-skill/SKILL.md`. A bare `SKILL.md` becomes the skill `imported-skill`. Absolute paths, `..` and hidden segments are rejected. The response also carries `warnings` and `dry_run`.
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
