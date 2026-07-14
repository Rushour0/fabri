# Fabri Agency Builder

Build a small multi-agent fabri agency from a single deliverable and an
observable acceptance gate. The skill is public and self-contained; it does
not depend on a private example or external prompt library.

## Install

For Claude Code, once this repository is published on GitHub:

```text
/plugin marketplace add Rushour0/fabri
/plugin install agency-builder@fabri-skills
/reload-plugins
```

Then ask: “Build an AI agency for changelog-to-release-notes production.” The
plugin exposes the namespaced skill `/agency-builder:agency-builder` and also
auto-triggers from its description.

For a local Claude Code check from the repository root:

```bash
claude plugin validate .
claude plugin validate skills/agency-builder
claude plugin marketplace add .
claude plugin install agency-builder@fabri-skills
```

For Codex CLI:

```bash
codex plugin marketplace add Rushour0/fabri   # or a local path during development
codex plugin add agency-builder@fabri-skills
```

This reads `.agents/plugins/marketplace.json` at the repository root, which
points at `skills/agency-builder/.codex-plugin/plugin.json` — the same
`SKILL.md` content Claude Code uses. Verify with `codex plugin list`; the row
should read `agency-builder@fabri-skills  installed, enabled`.

## Try the worked agency

From the repository root, first inspect the assembled tools and roles without
credentials:

```bash
fabri --config examples/agencies/changelog-release-notes/agent.yaml run --dry-run \
  "Create verified release notes from examples/agencies/changelog-release-notes/source/release_input.json at examples/agencies/changelog-release-notes/deliverables/release_notes.md."
```

Then set the provider key named in `agent.yaml` and run the same command without
`--dry-run`. It creates `deliverables/release_notes.md` through the writer
specialist and checks it through the verifier specialist and repair gate.

The example needs `ANTHROPIC_API_KEY` for a live multi-agent run and
`fabri[sqlite]` for the embedded memory backend. Its custom tools can be tested
without credentials; see the example README.
