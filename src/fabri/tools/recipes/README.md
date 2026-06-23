# tool recipes

Copy-paste-ready tool patterns. Each `*.json` is a complete `ToolManifest`;
each `*.py` (or `*.js`, `*.go`, ...) is the matching executable. Drop the pair
into your `tools/agent_tools/` and add the directory to your `agent.yaml`'s
`tools.manifest_dir`.

The point: lower the friction of adding fabri's most common tool shapes by
having them already written + tested. Not a registry the framework reads at
runtime — just a copy-from spot.

## What's here

| recipe | one-liner | language |
|---|---|---|
| `fetch_url` | `urllib`-based HTTP GET → text (HTML stripped) | python |
| `run_shell_safe` | whitelisted read-only shell commands | python |
| `git_diff` | wrap `git diff <args>` with output cap | python |
| `grep_dir` | recursive grep with file glob | python |
| `python_eval` | safe `python -c` style expression eval (math only) | python |

These are *patterns*, not security-audited primitives. Adjust the deny-lists
and sandbox checks to fit your threat model before exposing them to
untrusted input or a high-privilege agent.

## Adding a recipe

1. Pick a name (alphanumeric + underscore).
2. Write the manifest (`<name>.json`) + executable. Cap output. Refuse
   destructive operations explicitly.
3. Update this README's table.

Don't ship recipes that depend on a network service or unvendored library
unless you also document the install step in the recipe's header comment.
