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

Then describe the agency, including the target persona, the one deliverable,
specialist roles, proof-bar metric, and approval gate — the skill returns its
framing template and asks for anything missing rather than inventing it, so a
bare prompt like "build an AI agency for changelog-to-release-notes
production" gets a clarifying question back, not an immediate scaffold. The
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

Install `fabri` with the embedded SQLite memory backend first (use
`python3 -m pip` if your environment has no bare `pip` on `PATH`):

```bash
python3 -m pip install -e '.[sqlite]'
```

From the repository root, then inspect the assembled tools and roles without
credentials:

```bash
fabri --config examples/agencies/changelog-release-notes/agent.yaml run --dry-run \
  "Create verified release notes from examples/agencies/changelog-release-notes/source/release_input.json at examples/agencies/changelog-release-notes/deliverables/release_notes.md."
```

Then set `ANTHROPIC_API_KEY` (the provider key named in `agent.yaml`) and run
the same command without `--dry-run`. It creates `deliverables/release_notes.md`
through the writer specialist and checks it through the verifier specialist
and repair gate — read the verifier's own output, not just CLI exit success;
see the example README's "same proof bar" note.

The example's custom tools can be tested without credentials; see the example
README's "Test the real tool contract without a model" section.
