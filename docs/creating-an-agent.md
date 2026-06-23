# Creating an agent with this framework

This framework is two things: an importable Python library (`fabri`)
and a CLI (`cli.py`) that drives it from a YAML config. Creating a new agent
means writing a config + a tools directory — no changes to `fabri/`
itself.

## 1. Install it into your project

Install the package however you'd install any dependency — it does not need to
sit next to your project:

```bash
cd your-project
pip install fabri
# ...or from a local checkout while developing the framework itself:
pip install -e /path/to/fabri

docker run -p 6333:6333 qdrant/qdrant      # Qdrant on :6333 (or use the repo's docker-compose.yml)
export ANTHROPIC_API_KEY=...
```

Installing puts an `fabri` console command on your PATH, and makes the
package importable:

```python
import fabri
print(fabri.__all__)
```

Per-run state (traces + logs) is written to `.fabri/` in whatever
directory you run from — add it to your `.gitignore`. Set `$FABRI_HOME`
to point it elsewhere.

## 2. Write an agent.yaml

Every field has a default (see `fabri/config.py::DEFAULT_CONFIG`), so
only override what you need. Full schema:

```yaml
agent:
  name: my-agent
  max_steps: 10                 # loop budget; raise for multi-tool research tasks
  output_format: json           # format the MODEL is asked to emit (decompose): json (default,
                                # reliable) or toon (opt-in, fewer output tokens, json-fallback)

llm:
  # The MAIN orchestrator backend. Roles below inherit these defaults
  # for anything they don't override.
  provider: anthropic           # "anthropic" | "openai" | "openrouter"
  model: claude-sonnet-4-6
  max_tokens: 1024
  api_key_env: ANTHROPIC_API_KEY

  # Per-role overrides. Each role can run on a different provider with its
  # own API key; the four roles bill independently. Each entry may be:
  #   - omitted / null  -> role inherits everything from the main llm.*
  #   - a model-id string -> just swaps the model; provider stays
  #   - a dict          -> any subset of {provider, model, api_key_env,
  #                                       max_tokens, base_url, cache_messages}
  # Example: cheap narrator on OpenRouter, decompose on OpenAI mini.
  narrator:
    provider: openrouter
    model: anthropic/claude-haiku-4-5
    api_key_env: OPENROUTER_API_KEY
    max_tokens: 60
  decompose:
    provider: openai
    model: gpt-4o-mini
    api_key_env: OPENAI_API_KEY
  planner: null                 # falls back to decompose, then main

  # Legacy flat keys still work -- silently lifted into the role dicts
  # above when the dict form is absent. Prefer the new shape in new configs.
  # decompose_model: claude-haiku-4-5
  # narrator_model: claude-haiku-4-5
  # narrator_max_tokens: 60

tools:
  manifest_dir:                 # one path, or a list -- merged into one registry
    - builtin                   # framework's bundled tools (read_file/write_file/...)
    - tools/agent_tools         # your project's own tools, relative to cwd
  enabled: [read_file, write_file]   # null = every discovered tool
  sandbox_root: project          # read_file/write_file refuse paths outside this
  result_format: toon            # how tool results are fed INTO the model: toon (default,
                                 # fewer input tokens) or json
  decompose:
    enabled: false                # turn on for research-shaped tasks
    max_subquestions: 5

memory:
  collection: my_fabri    # separate Qdrant collection per agent
  qdrant_url: http://localhost:6333
  top_k: 5
  similarity_threshold: 0.85     # dedup threshold for guideline merging
  promotion_threshold_sessions: 3
  guideline_max_tokens: 30
```

Run it:

```bash
fabri --config agent.yaml run "do the task"
```

`builtin` (or `builtin:tools`) in `manifest_dir` resolves to the framework's
own bundled tools wherever the package is installed — so you never hardcode a
path to the fabri checkout. Every *other* path in the config
(`manifest_dir` entries, `sandbox_root`) resolves relative to **the directory
you run the command from**, not the config file's location — run from your
project root.

### Token efficiency: TOON

Tool results are fed into the model in [TOON](../src/fabri/toon.py)
(Token-Oriented Object Notation) by default — a compact, indentation-based
encoding of JSON that drops braces and, for uniform arrays, the repeated keys
(one header row instead). A typical tabular result is ~30–40% fewer characters
than JSON; the trace/logs keep the raw JSON, so only the copy in the model's
context shrinks. The framework encodes this itself, so there's no model
reliability risk — flip it off with `tools.result_format: json`.

The reverse (`json → toon → llm → toon → json`) is opt-in: set
`agent.output_format: toon` to ask the model to *emit* TOON (saving output
tokens on `decompose`), but it always falls back to parsing JSON if the model
doesn't comply — so the default stays `json` for reliability. One seam is
always JSON regardless: the providers' **native tool-call arguments**, which
the API returns as JSON (using TOON there would mean giving up native tool
calling). `fabri.toon.encode` / `.decode` are public if you want them in
your own tools.

## 3. Pick your tools

Every tool is a JSON manifest + an executable, auto-discovered by globbing
`*.json` in each `manifest_dir`. Nothing about the registry is Python-specific
or even fabri-specific — see `src/fabri/tools/examples/` for the
shape (`echo` in Python, `sum` in Go, `read_file`/`write_file` with a
path-jail, `web_search` calling out to Tavily).

A manifest:

```json
{
  "name": "my_tool",
  "description": "One sentence the LLM uses to decide when to call this.",
  "command": ["python3", "my_tool.py"],
  "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
  "output_schema": {"type": "object"},
  "timeout_s": 10
}
```

The script reads one JSON object from stdin, prints one JSON object to
stdout, and uses its exit code to signal success/failure:

```python
import json, sys
args = json.loads(sys.stdin.read())
print(json.dumps({"ok_field": "..."}))
# exit 0 = ok=true, the runner wraps your JSON in {"ok": true, "result": ...}
# exit != 0 = ok=false, same wrapping but {"ok": false, "error": ..., "result": ...}
```

The runner (`fabri/tools/runner.py`) normalizes timeouts, nonzero
exits, and malformed-JSON output into the same `{ok, error?, result?,
stderr?}` shape — your tool script never needs to worry about how the agent
loop reports failure, only about its own stdout contract.

**Sandboxing file access**: `read_file`/`write_file` resolve every path
against `$FABRI_SANDBOX_ROOT` (set by the CLI from `tools.sandbox_root`) and
reject anything that escapes it — `path.resolve().is_relative_to(root)`. If
you write your own file-touching tool, follow the same pattern; the registry
itself enforces nothing, the discipline lives in each tool script.

**Reusing the project's own validation, not reinventing it**: if your
project already has a build/codegen step that would catch a bad file (a
schema, a compiler, a linter), wrap *that* as a tool instead of writing new
validation logic. See `tools/agent_tools/validate_content.py` in this
project's `ludexel` integration (the fabri repo doesn't ship a
copy — it's project-specific) — it shells out to `tools/generate_all.py`,
captures stdout/stderr as JSON, and exits with the same code, so the agent
gets a real pass/fail from the actual compiler rather than an approximate
shape-check.

## 4. Decide whether you need `decompose`

Set `tools.decompose.enabled: true` for tasks that benefit from being broken
into sub-questions first (research-shaped tasks: "find out X", "compare Y").
It's a synthetic tool name — no manifest needed — handled inline in
`core/agent.py` via a second LLM call, not a sub-agent. Leave it off for
narrow, single-purpose agents; it adds a step and isn't useful if the task is
already concrete ("write this exact file").

## 5. Using it as a library instead of the CLI

Everything `cli.py` does is just composition over the public API — call it
directly if you want programmatic control (e.g. invoking the agent from your
own script or service):

```python
from fabri import run_agent, QdrantMemoryStore, build_llm, build_tool_defs, build_tools
from fabri.config import load_config

config = load_config("agent.yaml")
store = QdrantMemoryStore(
    url=config["memory"]["qdrant_url"], collection=config["memory"]["collection"]
)
tools = build_tools(config["tools"])   # resolves `builtin`, agent-as-tool entries, the enabled filter
llm = build_llm(config, build_tool_defs(tools, config["tools"]["decompose"]))

result = run_agent("do the task", llm, tools, store, max_steps=config["agent"]["max_steps"])
```

`build_tools`/`build_llm`/`build_tool_defs` are the same helpers `cli.py` uses,
so a programmatic agent behaves identically to the CLI one.

To skip Qdrant and run with the in-process sqlite-vec backend instead — same
interface, no other code changes:

```python
from fabri import SqliteMemoryStore

store = SqliteMemoryStore(
    path=".fabri/memory.db", collection=config["memory"]["collection"]
)
```

## 6. Worked example: a content-authoring agent for a game project

`ludexel` (a GBA game) wires this framework up as a story/content authoring
agent:

- `tools.manifest_dir` lists both the framework's generic tools
  (`read_file`/`write_file`, sandboxed) and a ludexel-specific
  `tools/agent_tools/validate_content` tool that runs the game's own
  `tools/generate_all.py` codegen pipeline.
- `tools.sandbox_root: project` keeps every write confined to
  `project/{story,characters,maps,gfx}/` — the agent can never touch `src/`,
  `.git`, or anything outside the content directories.
- The loop is: write/edit a `project/story/arcs/*.json` or
  `project/characters/*.toml` file with `write_file`, then call
  `validate_content` to actually compile it through the real generators —
  catching broken node references, unknown flags, or schema violations with
  the project's own authoritative tooling instead of a hand-rolled validator.

See `ludexel/.agent/story_agent.yaml` for the full config.
