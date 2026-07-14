# Changelog-to-release-notes agency

This is the agency-builder walking skeleton. It turns one structured changelog
into one Markdown release-notes file. The fixed team is deliberately small:

```text
orchestrator -> release_research -> release_writer -> release_verifier
                                      |                    |
                                      +---- repair <-------+
```

The three specialist configs are static agents-as-tools. The writer calls a
deterministic JSON-manifest tool to materialize the deliverable; the verifier
calls another manifest tool, and `scripts/check_release_notes.py` checks the
same headings and source items as a trusted repair command. Treat these as
two independent checks of the same proof bar, not a hard gate on the CLI's
final success: fabri's repair loop reruns the agent on a failing host check up
to `max_attempts`, but the parent's last response still determines the run's
reported outcome — a model that reports success after repair is exhausted is
not automatically overridden. Read the verifier's own output
(`release_verifier`'s JSON verdict, or `check_release_notes.py`'s `ok` field)
rather than trusting the run's final message alone.

**What's actually verified here.** The committed
`deliverables/release_notes.md` was produced by invoking
`build_release_notes` directly (see "Test the real tool contract without a
model" below) and passes both `verify_release_notes` and
`check_release_notes.py`. It is not evidence of a completed multi-agent
`fabri run` — that needs `ANTHROPIC_API_KEY` and network access, which this
example has not been run with end to end. Do the dry-run first, and don't
describe a live run as having happened until you've actually made the live
provider call and can point at its session ID.

## Run

Install the local package with SQLite support and set an Anthropic key:

```bash
python3 -m pip install -e '.[sqlite]'
export ANTHROPIC_API_KEY=...
fabri --config examples/agencies/changelog-release-notes/agent.yaml run \
  "Create verified release notes from examples/agencies/changelog-release-notes/source/release_input.json at examples/agencies/changelog-release-notes/deliverables/release_notes.md."
```

The expected output is
`examples/agencies/changelog-release-notes/deliverables/release_notes.md`.
The live run requires Anthropic network access and an API key. Before spending
tokens, inspect the assembled roles with `--dry-run`:

```bash
fabri --config examples/agencies/changelog-release-notes/agent.yaml run --dry-run \
  "Create verified release notes from examples/agencies/changelog-release-notes/source/release_input.json at examples/agencies/changelog-release-notes/deliverables/release_notes.md."
```

## Test the real tool contract without a model

```bash
fabri tool validate examples/agencies/changelog-release-notes/tools/build_release_notes.json
fabri tool validate examples/agencies/changelog-release-notes/tools/verify_release_notes.json
FABRI_SANDBOX_ROOT="$PWD" fabri tool test build_release_notes \
  --args '{"source_path":"examples/agencies/changelog-release-notes/source/release_input.json","output_path":"examples/agencies/changelog-release-notes/deliverables/release_notes.md"}' \
  --dir examples/agencies/changelog-release-notes/tools
FABRI_SANDBOX_ROOT="$PWD" fabri tool test verify_release_notes \
  --args '{"source_path":"examples/agencies/changelog-release-notes/source/release_input.json","output_path":"examples/agencies/changelog-release-notes/deliverables/release_notes.md"}' \
  --dir examples/agencies/changelog-release-notes/tools
python3 examples/agencies/changelog-release-notes/scripts/check_release_notes.py \
  examples/agencies/changelog-release-notes/deliverables/release_notes.md
```

The prompts under `prompts/` are review copies of each config's
`agent.system_prompt`. Fabri's current YAML config accepts the prompt string;
it does not load a prompt Markdown path automatically.

## After a live run

`fabri run` prints a session ID; note it here (or wherever you report the
run) rather than only in scrollback:

```bash
fabri traces show <session_id>       # parent's own step-by-step trace
fabri report --since 1h              # aggregate cost/outcome for recent sessions
```

One caveat specific to this agency's shape: `release_research`,
`release_writer`, and `release_verifier` are static `tools.agents[]`
specialists, each a separate child session with its own session ID (visible
in the parent trace's tool-call result). `fabri traces show <session_id>` on
the *parent* ID shows what each specialist was called with and returned, not
the specialist's own internal step-by-step reasoning — show each child's own
session ID separately if you need that level of detail. Parent-reported cost
also does not currently include static specialist child cost (only
`spawn_subagent` fan-out rolls up automatically) — treat `fabri report`'s
number for this agency as the parent's cost floor, not the full run cost.
