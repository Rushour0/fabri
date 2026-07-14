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
calls another manifest tool and the parent also has the same proof bar as a
trusted repair command. That double gate is intentional: the specialist gives
the parent an actionable verdict, while `agent.repair` prevents a final success
when the file itself fails the host check.

## Run

Install the local package with SQLite support and set an Anthropic key:

```bash
pip install -e '.[sqlite]'
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
