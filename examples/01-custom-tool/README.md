# 01 · Writing and wiring a custom tool

**What you'll learn:** the fabri tool contract, how to wire a custom tool next
to the builtins, and how to shape a tool so it costs few tokens per step.

**Optimization methodology demonstrated:** *return the minimum + compact
encoding* — the fabri analog of the delta-compression / pre-compression ideas
in RetainDB/ByteRover (see `docs/optimization-methodologies.md`). A tool result
rides in the context on **every** subsequent step, so the tool returns the
top-N slice the agent asked for, as a flat TOON-friendly array, not the whole
tokenized document.

## The files

```
01-custom-tool/
├── agent.yaml                 # wires builtin + custom tools into one agent
└── tools/
    ├── word_frequency.json    # the manifest — the ONLY thing the LLM reads
    └── word_frequency.py      # the executable — stdin JSON → stdout JSON
```

## The tool contract

Every fabri tool is **a JSON manifest + an executable** that speaks one
contract: read one JSON object from stdin (the args), print exactly one JSON
object to stdout (the result), exit 0 on success / non-zero on error. The
executable can be in any language — here it's Python; the builtins include
Node and Rust examples too.

The manifest's `description` is the entire interface the model sees, so it
states what the tool returns and when to reach for the optional args. The
`input_schema`/`output_schema` are JSON Schema; `timeout_s` caps the run (the
runner kills the whole process group on timeout and caps stdout at 1 MiB).

## Validate and test it — before an agent ever calls it

fabri ships a tool-development gate. Run it from the repo root:

```bash
# Shape / schema / script-path check
fabri tool validate examples/01-custom-tool/tools/word_frequency.json

# Actually invoke it through the real runner (needs FABRI_SANDBOX_ROOT so the
# path jail is satisfied — the runner sets this from tools.sandbox_root at
# agent runtime; set it by hand when testing a tool in isolation):
FABRI_SANDBOX_ROOT="$PWD" fabri tool test word_frequency \
  --args '{"path": "examples/01-custom-tool/README.md", "top_n": 5, "min_length": 4}' \
  --dir examples/01-custom-tool/tools
```

`fabri tool test` builds a throwaway registry and calls the tool exactly the
way an agent would, printing `{ok, result?, error?}`. This is your inner loop —
never ship a tool an agent can't successfully call.

## Run the agent

```bash
export GEMINI_API_KEY=...     # or set llm.provider to a provider you have a key for
fabri --config examples/01-custom-tool/agent.yaml \
  run "which 5 words longer than 4 letters appear most in examples/01-custom-tool/README.md?"
```

## Design rules this example follows

From README §"Designing tools for low token cost":

- **One job, one tool.** `word_frequency` counts words. It does not also read,
  write, or grep — those are separate tools the agent composes.
- **Return the minimum.** Top-N records + totals, never the full token stream.
- **TOON-friendly shape.** A flat array of uniform `{word, count}` records
  encodes much smaller than nested objects under `result_format: toon`.
- **Cap your manifests.** `tools.enabled` lists only the two tools this task
  needs — every extra entry sits in the cached prefix and costs tokens forever.
- **Paths, not payloads** (not shown here, but the next step up): for a big
  result, write it to `.fabri/scratch/<id>.json` and return `{path, size}` so
  the bytes don't ride every step.

## Next

- `../02-parallel-fanout/` — spawn independent sub-agents in parallel.
- `../04-docker-sandbox/` — run this same tool inside a locked-down container.
