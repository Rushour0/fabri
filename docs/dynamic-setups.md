# Dynamic setups and tools

A product feeds fabri two things: an ordered list of layer directories, and a
policy that caps what those layers may turn on. fabri resolves the layers into
agent types, prompts, skills, context, data, plays and a tool set. Nothing in
fabri knows what a layer means to the product. One product might stack
`baseline, pack, org, project`; another might use `defaults, tenant`.

## Layers

Every layer has the same shape, and every part is optional:

```
<layer>/
  setup.yaml                     host settings plus `budgets:`; deep merged
  agents/<type>/agent.yaml       agent settings by type
  agents/<type>/prompt.md        replaces the prompt so far
  agents/<type>/prompt.append.md appended to the prompt so far
  skills/<name>/skill.yaml       plus an optional prompt.md
  context/<name>.md              always-on context; frontmatter `types: [...]` narrows it
  data/<name>.<ext>              reference data, not injected
  tools/tools.yaml               `sources:` and `allow:`
  tools/registered/<name>.json   tools added at runtime (see Registering tools)
  tools/descriptions.yaml        title and description overrides per tool
  plays/<name>/play.yaml
```

Merge rules, the same at every depth:

- `setup.yaml` and `agent.yaml` deep merge, and deeper scalars win.
- In `agent.yaml`, the `tools` and `skills` lists are unioned unless the deeper
  layer lists the key in `replace: [tools]`. `enabled: false`, or
  `setup.yaml` `agent_types: {<type>: false}`, turns a type off.
- `prompt.md` replaces, `prompt.append.md` appends.
- Skills, context, data and plays: the same name at a deeper layer replaces the
  shallower item. A marker file `<kind>/<name>.disabled`, or `enabled: false`,
  removes it.
- `tools.yaml` sources are unioned by name and `allow` entries are unioned.

Every resolved item records the layer it came from. `fabri setup show
--layers base org project --type chat` prints the merged view. Name a layer
with `NAME=DIR` when the directory name is not the name you want recorded.

Packs are not special. A pack is one more directory in the list.

`skill.yaml`:

```yaml
name: weekly-chart          # must match the directory
description: Build a weekly chart block.
types: [canvas]             # agent types that load it; [] means every type
tools: [chart_update]       # tools it needs; added to the type's tool list
```

A custom agent type becomes a route option when its merged `agent.yaml` has a
`route` block with `description` and `not_for`. `resolved.route_options()`
returns those in layer order, so a host router can offer them.

## Tool sources

`tools/tools.yaml`:

```yaml
sources:
  - kind: openapi
    name: pets
    spec: specs/petstore.yaml          # URL, path relative to this file, or inline mapping
    base_url: https://petstore.test
    methods: [GET, POST]
    headers: {Authorization: {secret_ref: "pets:prod", prefix: "Bearer "}}
    dry_run_header: {name: X-Dry-Run, value: "1"}
    approval: {pet_create: always_approve}
  - kind: http
    name: weather_now
    url: https://weather.test/now/{city}
    method: GET
    description: Current weather for a city.
    input_schema: {type: object, properties: {city: {type: string}}, required: [city]}
  - kind: mcp
    name: files
    command: [mcp-files, /srv/data]
  - kind: python
    name: score_lead
    callable: myapp.tools:score_lead
allow: [pet_list, pet_get, pet_create, weather_now, "mcp_files_*"]
```

An OpenAPI operation becomes one tool. The name is `<resource>_<action>` from
the path and method (`GET /pets/{id}` is `pet_get`, `POST /pets/{id}/adopt`
is `pet_adopt`); version segments and `strip_prefixes` are dropped, and a
collision gets a stable four-character suffix. The `operationId` stays as an
alias, so the registry, `allow` lists and approval maps accept either name.
`naming: operation_id` flips this, and `names: {opId: my_name}` overrides one.
DELETE operations are left out unless `methods` names DELETE or `allow` names
the operation exactly. Responses map to `{status: ok, data}`, `{status:
conflict}` for 409, and `{status: error, code}` for other 4xx and 5xx. Header
values come from secret refs or environment variable names and are never
logged.

A tool is kept only when a layer's `allow` entry matches it and the caller's
global allowlist matches it too. A layer can narrow, never widen.

Every tool has an approval class: `read` (GET by default), `write` (every
other method), or `always_approve`. fabri records it; the host's approval gate
reads it from `tool_set.approvals()` or `manifest.approval`.

## Integration

```python
from fabri import QdrantMemoryStore, run_agent
from fabri.runtime import build_run_llms, build_tool_defs
from fabri.setups import resolve_setup, resolve_tools, build_agent_config
from fabri.tools.policy import ToolPolicy

LAYERS = [("baseline", "setups/baseline"), ("pack", "setups/packs/sales"),
          ("org", "setups/orgs/acme"), ("project", "setups/orgs/acme/projects/q3")]
POLICY = ToolPolicy.from_file("setups/allow.yaml", base=ToolPolicy.permissive())


def answer(agent_type: str, message: str) -> dict:
    # Both calls are cached by a content hash of the layer files, so an
    # edit or a newly registered tool shows up here with no restart.
    resolved = resolve_setup(LAYERS)
    tools = resolve_tools(resolved, policy=POLICY)
    config, registry = build_agent_config(
        resolved, agent_type, {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        tool_set=tools)
    defs = build_tool_defs(registry, config["tools"]["decompose"])
    return run_agent(
        message, tools=registry, store=QdrantMemoryStore(collection="acme"),
        system_prompt_prefix=config["agent"]["system_prompt_prefix"],
        max_steps=config["agent"]["max_steps"],
        tool_retrieval_enabled=True,
        tool_retrieval_always_include=tuple(config["tools"]["retrieval"]["always_include"]),
        **build_run_llms(config, defs))


def needs_approval(tool_name: str) -> bool:
    tools = resolve_tools(resolve_setup(LAYERS), policy=POLICY)
    return tools.approval_for(tool_name) != "read"
```

`config["setup"]` carries the type, the content hash, the loaded skills, each
tool's approval class and any budget warnings.

## Runtime edits, import and lint

`fabri.setups.put_item(layer_dir, kind, name, content)` validates and writes
one item atomically; `delete_item` removes it and `set_disabled` writes a
marker that also hides shallower copies. Kinds are `setup`, `agents`, `skills`,
`plays`, `context`, `data` and `tools`. Validation covers slugs, names that
match directories, required fields, tool names against the global allowlist,
budgets from that layer's `setup.yaml` `budgets:` (`skill_prompt_chars`,
`context_chars`, `prompt_chars`, `data_bytes`), and `slop_report` warnings.
Pass `validators={"plays": fn}` to add product checks. The `Setups` class
bundles a layer list, a policy and validators.

`fabri setup import --from DIR --format skill-md|fabri --into LAYER_DIR
[--dry-run]` imports skills and reports created, updated, skipped (identical
content) and invalid. Running it twice changes nothing.

`fabri setup lint --layers ...` reports budget breaks, writing tells, unknown
keys, and skills or agent types that name tools the resolved set lacks.

## Registering tools

A host can add a tool without editing `tools.yaml`:

```python
from fabri.tools.registration import register_tool
register_tool("setups/orgs/acme/projects/q3", {
    "name": "crm_lookup", "kind": "http", "description": "Look up a contact by id.",
    "endpoint": "https://api.crm.example/contacts/{contact_id}", "method": "GET",
    "input_schema": {"type": "object", "properties": {"contact_id": {"type": "string"}},
                     "required": ["contact_id"]},
    "auth": {"secret_ref": "crm:prod", "header": "Authorization", "prefix": "Bearer "},
    "approval": "read", "version": 1,
}, actor="ops@example.com", policy=policy)
```

`kind` is `http` (`endpoint`), `openapi` (`source: {spec, operation_ids}`),
`mcp` (`server: {command}` or `{url}`) or `python` (`callable`). Registration
checks the name, the input schema, that `auth` holds a secret ref or an
environment variable name and never a raw value, and the policy: allowlist,
`allowed_kinds`, `allowed_hosts`, DELETE (`allow_delete`) and
`python_modules`. The default policy allows http and openapi only, with no
DELETE and no python.

Each tool is stored as `tools/registered/<name>.json`. Registering a higher
version replaces it; the same version with different content is refused.
Every register, unregister, enable and disable appends a line to
`tools/registered/_audit.jsonl` with the actor, action, name, version, time and
content hash. The resolver merges registered tools with `tools.yaml`, so the
global cap and hot reload apply the same way.

```
fabri tools register --layer DIR --file decl.json [--policy policy.yaml]
fabri tools unregister --layer DIR --name N
fabri tools enable|disable --layer DIR --name N
fabri tools list --layers ...     # SOURCE column: declared, registered or openapi
```

## Catalog, discovery and probing

Each tool has a catalog entry: name, aliases, title, description, resource,
action, method, path, tags, params (type, required, enum), approval, source,
examples and options. The title is the OpenAPI summary when it is short, or
"List pets" / "Adopt a pet" built from the resource and action. The
description has short lines for what the tool does, which sibling tool to use
instead, key params (with enum values or probed options), errors by status code
with reason enums, and the approval class. Titles are capped at 60 characters
and descriptions at 1,200. A layer's `tools/descriptions.yaml` overrides
either one per tool and the entry records which layer won.

The catalog text becomes the manifest description, so fabri's tool retrieval
(top-k by embedding over title and description) ranks on it. `build_agent_config`
adds two meta tools that are always present:

- `find_tools(query, k)` returns names with one-line descriptions
- `describe_tool(name)` returns the full schema, examples and options

Either one asks the agent loop to add what it returned to the provider tool
list, so the model can call a tool in the same run even when retrieval hid it.

`fabri tools sync --layers ... --global-allow FILE` prints added, removed and
changed tools against the last sync and saves `catalog.json` in the deepest
layer's `tools/.fabri-state` (hidden files never change the content hash).

`fabri tools probe --layers ... [--ops ...]` (or `fabri.tools.probe.refresh_probes`
from a scheduled job) calls only GET and OPTIONS operations, plus operations
marked `x-fabri-probe-safe: true`. Every call carries the policy's dry-run
header (`X-Dry-Run: true` by default), a short timeout and a per-op call cap.
It saves the response shape, a redacted sample, and the values of id, slug and
enum-like fields as options, which also fill the path params of write
operations on the same resource. Keys such as email, phone, token, password and
address are never stored, and neither are values that look like an email, a
phone number or a credential.

`fabri tools describe --layers ... --llm '{provider: openai, model: gpt-5-mini}'`
rewrites titles and descriptions with any fabri `llm:` block, once per tool,
cached by a hash of the operation schema and its probe examples. The prompt
holds only the schema and redacted examples. A rewrite that names a param or
status code the schema does not declare is dropped. A rewrite that
`slop_report` flags is regenerated once, then dropped. Dropped rewrites leave
the schema text in place.
