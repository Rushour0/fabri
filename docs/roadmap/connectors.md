# Track C — Connectors: agents that run *inside* the company

> **Status:** scoping / not started. This is a forward-feature initiative, not a
> shipped capability. It needs a proper `agentic-pm` decomposition pass before
> any code lands — this doc frames the "why" and the architecture so that pass
> starts from a decided shape, not a blank page.
>
> **Card prefix:** `C1`, `C2`, … (see [ROADMAP.md](../ROADMAP.md) for the
> track/card convention). Proposed as a new **Track C** alongside F/S/A/R/O/M/X/B.

## Why this exists

Today a fabri agency is demonstrated in Studio and runs against a **sandboxed
workspace** (`$FABRI_SANDBOX_ROOT`, per Track S). That proves the engine, but the
product thesis is bigger: a fabri "company" is a **company of AI agents that
operates inside a real company's own surfaces** — its code repositories, its
Slack, its issue tracker, its docs. The value isn't a chat demo; it's a crew that
opens a PR against *your* repo, answers a question in *your* Slack, and reports
what it cost to do so.

This is also the sharpest **counter-position vs. control-plane SaaS competitors**
(see [`paperclip.inc` teardown] in strategy notes): they sell governance over a
walled roster of agents. fabri's wedge is **self-improving + COGS-instrumented +
open-engine agents that run in the customer's real tools** — you don't move your
work to the agents, the agents come to your work. Connectors are the mechanism
that makes that literal.

## What "inside the company" means concretely

Three first-class surfaces, in priority order:

1. **Code repositories** — an agency clones a target repo, works on a branch in
   an isolated workspace, and opens a PR back. fabri already shallow-clones repos
   in `src/fabri/agency_registry.py` (for `fabri new agency --from gh:…`); the
   connector generalizes that from "fetch a template" to "check out, mutate on a
   branch, push, open PR."
2. **Slack** — an agency reads designated channels/threads and posts replies or
   status, so a crew can be @-mentioned and answer in-place. Pairs naturally with
   the existing **ask-user primitive** (Track A): a clarifying question can route
   to a Slack thread instead of the Studio UI.
3. **Generic issue/doc surfaces** — Linear/GitHub Issues/Notion, same pattern:
   read context in, write an artifact (a triaged issue, a drafted doc) out.

## Architecture (fits existing primitives — no new engine)

Connectors are **builtin tools**, not a new subsystem. They ride the contract the
engine already has:

- **Tool contract.** Every fabri tool is a subprocess speaking `{ok, error?,
  result?}` JSON over stdin/stdout (see `src/fabri/tools/`, e.g.
  `agent_runner_tool.py`, `registry.py`). A `git_connector` tool and a
  `slack_connector` tool are just two more builtins on that contract — an agency
  gains them by listing them in its manifest, exactly like any other tool.
- **Sandbox boundary.** Connectors operate *through* the Track S sandbox
  (`$FABRI_SANDBOX_ROOT` / the future `Sandbox` interface). "Work inside a repo"
  = the sandbox root is a checkout of the customer's repo; the git connector is
  the only thing allowed to push. This keeps the blast radius the sandbox already
  defines.
- **Ask-user reuse.** Slack read/post reuses Track A plumbing so a connector can
  both *deliver* an answer and *ask* for one in the same channel.
- **MCP option.** `src/fabri/tools/mcp_client.py` already exists — some surfaces
  (Slack, GitHub, Linear) have MCP servers. Where a good one exists, a connector
  can be a thin MCP wiring rather than a hand-rolled tool. Decision per surface.

### The missing piece: a credential store

The one genuinely new component. Connectors need per-surface secrets (a GitHub
token/App installation, a Slack bot token, an OAuth grant) that must **not** live
in agency YAML or the sandbox. Proposed:

- A `CredentialStore` protocol (env-backed for local/dev; a real secret manager
  for hosted) that a connector tool resolves by **named handle** — the agency
  references `github:acme` / `slack:acme-eng`, never the raw secret.
- Scope + audience are attached to the handle, so a connector physically cannot
  reach a repo/channel outside its grant.

## Guardrails (non-negotiable, gate the whole track)

- **Least privilege.** A connector grant names exact repos / channels; default
  deny. No org-wide tokens handed to an agency.
- **Human-gated writes.** Outbound actions (push, PR, Slack post) are opt-in per
  agency and should default to **propose, don't apply** — a PR a human merges, a
  drafted message a human sends — mirroring the loop's gated-PR model.
- **COGS on every action.** Connector calls are instrumented like every other
  fabri cost, so "the crew that watches this repo" has a visible ₹/run — the
  COGS wedge extends to real-world work, not just token spend.
- **Auditability.** Every outbound connector action is traced (Track X / OTel
  export) with the credential handle, target, and diff/message.

## Proposed cards (for the `agentic-pm` pass to refine)

- **C1 • Credential store** • protocol + env-backed local impl + named-handle
  resolution. Blocks everything else.
- **C2 • Git connector** • clone → branch → commit → push → open PR, through the
  sandbox, propose-don't-apply default. Walking skeleton of the whole track.
- **C3 • Slack connector** • read channel/thread + post reply, reusing Track A;
  @-mention entrypoint.
- **C4 • Guardrail + COGS wiring** • least-privilege enforcement, human-gate
  toggle, per-action cost + OTel trace. Gates C2/C3 for real use.
- **C5 • Generic surface adapter** • Linear/Issues/Notion via MCP where available.

## Explicitly out of scope for now

No connector code this round. No hosted credential manager (env-backed only in
the skeleton). No multi-tenant secret isolation until there's a second real
tenant. This doc is the frame; the `agentic-pm` pass turns C1–C5 into vertical
slices with acceptance criteria and a metric each.
