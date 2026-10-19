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

Every route requires the service's existing auth (`FABRI_SERVICE_TOKEN` / Studio login). Writes need the `admin` role.

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

Scope string: `baseline` | `pack:<name>` | `org:<org>` | `org:<org>/project:<project>`. Kind: `agents` | `skills` | `context` | `data` | `tools` | `plays`.

## Rules

- Path segments are validated. Writes never escape the layer root.
- Secrets never appear in responses. Tool auth is shown as a secret reference name only.
- Every write appends to `<layer>/.fabri-audit.jsonl` with actor, action, scope, kind, name, content hash and timestamp.
