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

- The parent's registry passes its own tool names to the spawn. Any name the
  parent does not have is dropped. If nothing is left, the spawn is refused.
- The child builds its tools from its own config as usual, then removes every
  tool the scope does not allow. A name in the scope that the child's config
  lacks is reported under `tool_scope.unmatched` and ignored.
- `read_only` keeps tools whose manifest says `approval: read`, plus the
  bundled read tools (`read_file`, `grep`, `list_dir`, `file_stats`,
  `regex_lines`, `fetch_url`, `web_search`, `echo_tool`, `sum_tool`,
  `find_tools`) when their manifest has no approval class.

The scope can only remove tools. It is applied in
`fabri/tools/agent_runner_tool.py` right after `build_tools`, and the code is
in `fabri/tools/tool_scope.py`.

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
The loop enforces part of this. In one `parallel_group`, a spawn whose scope
allows any tool that is not read-class counts as a writer. The first writer
runs alongside the readers; any later writer in that group leaves it and runs
serially before the group starts.

Spawns with no `tool_scope` keep the old behavior and still fan out together,
because existing agencies rely on that and give each child its own sandbox. If
you fan out unscoped children that can write, make sure their sandboxes do not
overlap.

## Summary envelope

Each child returns:

- `final_text`: its summary, capped at `agent.subagent.summary_max_tokens`
  tokens (default 2000; 0 turns the cap off).
- `outcome`, `usage`, `session_id` and `trace_path`, always.
- `summary_truncated`: true when the summary or the structured output was cut.
  When it is, `final_text_tokens` or `structured_output_dropped_tokens` gives
  the original size. The full text is in the child's trace.

The cap cuts only the summary field, so the envelope stays valid JSON. Code is
in `fabri/tools/subagent_envelope.py`. `spawn_subagent` applies the default cap
again if a runner sent an envelope that was never capped.

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
- The profile is cached under a hash of the task kind, the parent's tool names
  and a factory version. Pass `task_kind` (for example `"triage failing
  test"`) to let many tasks share one profile. Pass a `ProfileCache(dir)` to
  keep profiles on disk; `default_cache_dir()` is `.fabri/specialists` under
  `FABRI_HOME`. On a hit the designer does not run.

`spawn_args(profile, task, config_path)` builds the `spawn_subagent` call: the
profile's prompt goes in `system_prompt_inline`, the task brief goes in `task`,
and the profile's tools go in `tool_scope`.

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
