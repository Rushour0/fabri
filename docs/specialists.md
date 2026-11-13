# Specialist sub-agents

A parent agent can hand a self-contained task to a child agent that sees only
the tools that task needs. The parent's context stays small because it never
loads the child's tools or reads the child's transcript. It gets back a short
summary and a pointer to the child's trace.

This is opt-in. Nothing in the agent loop builds specialists on its own, and
`spawn_subagent` behaves as before when you leave the new arguments out.

There are four pieces:

1. A per-spawn tool scope on `spawn_subagent`.
2. A capped summary envelope that every child returns.
3. A task brief in the prompt kit.
4. A specialist-profile factory with a cache.

A small eval harness compares this approach with giving the parent every tool
and with tool retrieval.

## Tool scope

`spawn_subagent` takes an optional `tool_scope`: a list of tool names, or the
preset `read_only`.

- The value must be a list of non-empty strings (a single string also works).
  A dict, number, boolean, null or a list holding anything else is refused, so
  a malformed scope never turns into an unscoped child.
- The parent's registry passes its own tool names to the spawn in
  `FABRI_PARENT_TOOLS`. It does this for any tool that runs
  `spawn_subagent.py`, whatever the tool is called. Any name the parent does
  not have is dropped. If nothing is left, the spawn is refused. If a scope is
  given and that list is missing or unreadable, the spawn is refused too.
- The child builds its tools from its own config as usual, then removes every
  tool the scope does not allow. A name in the scope that the child's config
  lacks is reported under `tool_scope.unmatched` and ignored.
- The child then writes the names it kept to `FABRI_TOOL_CEILING`. Every agent
  it starts, spawned or a static `tools.agents[]` child, inherits that and
  drops every tool not named in it, then passes its own (possibly smaller)
  list down. A grandchild can never see more tools than its scoped ancestor,
  even if it is spawned without a scope. The registry forwards the ceiling to
  every tool explicitly, so sandboxes that do not copy the host environment
  (DockerSandbox) still pass it on.
- The ceiling matches by tool name. A descendant config that defines its own
  tool under an allowed name (say a `read_file` that runs a shell) keeps it.
  Keep tool names consistent across the configs a scoped tree can reach.
- `read_only` keeps tools whose manifest says `approval: read`, plus the
  bundled local read tools (`read_file`, `grep`, `list_dir`, `file_stats`,
  `regex_lines`, `echo_tool`, `sum_tool`) when their manifest has no approval
  class and runs fabri's own bundled script. A config tool that reuses one of
  those names with a different command does not count. `fetch_url` and
  `web_search` are not in the preset because they can send data out.

The scope can only remove tools. It is applied in
`fabri/tools/agent_runner_tool.py` right after `build_tools`, and the code is
in `fabri/tools/tool_scope.py`.

The `decompose` meta tool is not in the registry, so a scope does not remove
it. A scoped child can still split its task with `decompose` when its config
enables it.

The model can ask for a scope, but code decides what it gets: the request is
cut down to the parent's tools and then to the child's config. The factory
below sets the scope from code and never lets a model write it directly.

### Approval

Narrowing deletes tools from the child's registry and leaves its approver in
place. A child still never approves its own calls: an `always_approve` tool
left in scope asks the parent's human over the ask-user socket, or is refused
when there is no socket. `spawn_subagent` itself is now marked
`approval: write`. fabri records that class for hosts; it does not ask a human
for plain `write` tools. If your host wants a person to approve every spawn,
ship a copy of `spawn_subagent.json` with `"approval": "always_approve"` in
your own manifest directory. Policy `approval_floor` entries apply to
configured sources, not to bundled manifests, so they will not raise it.

There is no `FABRI_APPROVAL_MODE` setting in fabri today. Children inherit
approval through the ask-user socket as described above.

### One writer at a time

Parallel children should either only read or touch nothing the others touch.
The loop enforces this. In one `parallel_group`, a spawn is a reader only when
it provably reads:

- with a `tool_scope`: the scope gives it read tools only, judged against the
  parent's registry (`read_only`, or names the parent knows as read-class);
- without a scope: its child config loads, every tool it builds is
  read-class, and it has no spawn tool, no `tools.agents[]`, no MCP servers
  and no `tools.sources`.

Every other spawn is a writer: a config that fails to load, a malformed
scope, an unknown tool, or any tool that writes or reaches the network. The
first writer runs alongside the readers; any later writer in that group leaves
it and runs serially before the group starts.

`examples/02-parallel-fanout` keeps fanning out: its `worker.yaml` only has
`read_file`, `list_dir` and `grep`. A child config with `write_file`, `bash`,
`fetch_url` or `spawn_subagent` now runs one at a time within a group.

## Summary envelope

Through `spawn_subagent`, each child returns:

- `final_text`: a prose summary capped at the parent's
  `tools.spawn_summary_max_tokens` (default 2000; 0 turns the cap off). The
  parent's registry passes that budget to the spawn, and the spawn applies it
  on the parent's side, so the child's own config cannot change it. A bad
  value fails when tools are built, before anything is spawned.
- `outcome`, `usage`, `session_id` and `trace_path`, always.
- `summary_truncated`, always. When it is true, `final_text_tokens` or
  `structured_output_tokens` gives the original size, or
  `memory_actions_applied_dropped` says side data was removed. The full text is in the child's trace.

Typed output:

- When the child config declares `agent.response_schema`, a `final_text`
  that parses as JSON and the `structured_output` are kept whole. The spawn
  reads the schema from the child config file, not from what the child says.
- Without a schema, JSON gets the same budget as prose. An over-budget value
  becomes `{"truncated": true, "text": "<cut JSON text>"}`, so it stays valid
  JSON and the parent can see it was cut.
- If the envelope is still over 900 KiB, the spawn fails with an error that
  carries `outcome`, bounded `usage`, `session_id` and `trace_path`, so the
  parent still records the child's cost.

`usage` and `memory_actions_applied` are reduced when either is over 8 KiB:
`usage` keeps its token and cost fields, and the actions list is replaced by a
count.

Static `tools.agents[]` children are not capped. They return their full
envelope as before.

Code is in `fabri/tools/subagent_envelope.py` and
`fabri/tools/examples/spawn_subagent.py`.

The runner's 1 MiB stdout limit is still there as a last resort for any tool.
When it fires, the tool result now says the output was too large and includes
a short preview, instead of reporting malformed JSON.

## Task brief

`fabri.builder.prompt_kit.render_task_brief` renders the brief a child gets as
its task. It has four parts, in this order:

1. Objective: the task itself. This is the only part a model normally writes.
2. Output format: a short summary the parent can act on, with evidence and
   anything left undone.
3. Tools: the child's tool list with an optional one-line note per tool.
4. Boundaries: stay on this task, and stop and say so if a needed tool is
   missing.

These four parts come from Anthropic's write-up on its multi-agent research
system, which found that children given vague briefs duplicated work or went
off course.

The prompt skeleton used to open with `ABSOLUTE SCOPE` and close with
`HARD INVARIANTS`. Those sections are now `SCOPE` and `WORKING RULES`, and each
rule says why it exists, so the model can judge the rare case where it does
not fit.

## Specialist factory

`fabri.builder.specialists.make_specialist(task, parent_tools, llm)` returns a
profile with a name, a system prompt and a tool list.

- A designer model sees the task and the parent's tool names. It returns a
  role, what the specialist owns, the tools it needs and a note per tool.
- Code drops any tool name the parent lacks, sorts the list and renders the
  system prompt from the prompt-kit skeleton.
- Designer text is flattened to one line, stripped of `#`, backticks and
  angle brackets, and capped (role 60 characters, at most 3 ownership lines
  and one note per tool, 120 characters each). That stops it from adding
  sections to the prompt. It does not make the words safe: a task that carries
  an injection can still steer the role text, so treat the factory's output
  like any other model output.
- The profile is cached under a hash of the task kind, the parent's tool
  names, the designer `model`, a hash of the child config file
  (`config_path`), the `policy` in force and a factory version. Pass
  `task_kind` (for example `"triage failing test"`) to let many tasks share
  one profile. Pass a `ProfileCache(dir)` to keep profiles on disk;
  `default_cache_dir()` is `.fabri/specialists` under `FABRI_HOME`. On a hit
  the designer does not run.
- A cached profile is checked on load. Its key must be 16 hex characters and
  match its file name. Every designer field must be exactly what the cleaning
  and length limits above produce, the tool list must be sorted and unique,
  notes may only name its tools, and the name must follow from role and key.
  Its prompt must equal what code renders from those fields and match the
  stored hash, and its tools are intersected with the current parent's tools.
  A file that fails any check is ignored.

`spawn_args(profile, task, config_path)` builds the `spawn_subagent` call: the
profile's prompt goes in `system_prompt_inline`, the task brief goes in `task`,
and the profile's tools go in `tool_scope`. Extra keyword arguments (for example
`parallel_group` or `timeout_s`) are passed through, but they cannot replace
`tool_scope` or the system prompt.

The system prompt depends only on the profile, so it is byte-identical across
spawns of the same profile. The task-specific text goes in the user message.
That keeps the provider's prompt cache warm across children. The agent loop
already places retrieved memory after tools and policies in the system prompt,
and the specialist prompt sits in the identity slot ahead of both.

## Eval harness

`fabri.benchmarks.specialist_eval` compares three arms on 6 synthetic tasks
over 30 read-only lookup tools:

- `direct`: the parent sees all 30 tools.
- `retrieval`: the parent sees the top 6 tools by word overlap with the task.
- `specialists`: the parent sees one `delegate` tool that builds a profile,
  runs a child with the narrowed tools and returns a capped envelope.

It reports success rate, cost and the parent's peak context in tokens.

Offline, with a scripted model and a flat illustrative price:

```
uv run python -m fabri.benchmarks.specialist_eval
```

A recent offline run gave:

```
arm           success   cost_usd  parent_peak_tok  llm_calls
direct           1.00     0.0549             3969         12
retrieval        1.00     0.0393             2213         12
specialists      1.00     0.0798              301         30
```

The scripted model follows each task's plan, so these numbers show the context
mechanics and nothing about how well a real model picks tools. Specialists cut
the parent's context by about 13x here and cost more in total, because every
task adds a designer call and a child loop.

Live, with a real model (this costs money):

```
uv run python -m fabri.benchmarks.specialist_eval --live --config path/to/agent.yaml
```

Every role (parent, designer, child) uses that config's main `llm` block.
Add `--tasks 2` for a cheaper smoke run and `--json` for the full report. One
full live run is about 18 parent loops plus 6 designer calls and 6 child
loops, roughly 150k input tokens. Expect well under $1 on a Sonnet-class or
Gemini 2.5 Pro model; more if the model takes extra steps. The live path has
not been run yet.

The harness's retrieval arm uses word overlap, not fabri's embedding
retrieval, so it runs without an embedding model. Its numbers can differ from
what `tools.retrieval` would pick.
