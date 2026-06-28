# Why fabri exists

**fabri is the self-improving agent engine you build products on. Describe a
product; fabri scaffolds the agents, tools, and prompts — and they get cheaper
and more reliable every run.**

## The problem: frozen prompts

Most agent frameworks make you hand-write and freeze the prompt, hand-glue the
tools, and pay full price on every run. The prompt never learns from what the
agent actually did, so the same failure costs you again next week.

fabri inverts that:

- **Prompts grow from traces.** A failure in session N becomes retrievable
  context in session N+1 — no one edits the prompt by hand. That loop (trace →
  analyze → compress → dedup → promote → retrieve) is the engine's core.
- **Tools are a uniform contract.** A tool is a JSON manifest next to an
  executable in any language: stdin gets JSON args, stdout returns JSON, the
  runner normalizes errors. Agents compose as tools of other agents through the
  same contract.
- **A builder turns intent into a running agent.** You don't start from a blank
  `agent.yaml` and a blank prompt. You describe what you want and scaffold from
  there.

## Two layers

fabri is deliberately split so the hard, reusable machinery is separate from the
fast path to a new product.

**The engine** — the substrate that runs and learns:

- the frugal ReAct step loop with per-role LLMs (orchestrator / decompose /
  planner / narrator)
- the self-improving memory loop (retrieve → run → mine → dedup → promote)
- the sandbox and the polyglot stdin/stdout tool contract
- agents-as-tools, with hierarchical cost tracking and budgets

This is today's `core/`, `orchestrator/`, `memory/`, and `tools/`.

**The builder** — the product factory on top of the engine:

- **ideator** — an idea in plain language becomes a reviewable agent spec
  (config, prompts, the tools to build)
- **tool-writer** — a description or a function signature becomes a real,
  schema-tightened tool you can validate and test locally
- **prompt-kit** — a proven prompt skeleton and a user-prose / machine-memory
  output split, instead of a blank file
- **skills** — reusable bundles of prompt + tools + config you install into a
  project so a capability carries across products
- **service** — the whole thing packaged as a self-contained instance any host
  or language can call to spawn its own agents

The builder is tracked as **Track B** in [ROADMAP.md](./ROADMAP.md). Its job is
one sentence: **building a new product on fabri should be faster, not slower.**

## Honest COGS

A token spent is a token billed. The base system prompt steers every run toward
fewer, better-aimed actions; the defaults make the cheap path the default path;
and every run emits token totals plus `cost_usd` (priced per model) for the run
and its whole sub-agent subtree. As memory matures, the cost per session falls.
Cost is surfaced at every level so a host can track COGS without parsing logs.

## Where fabri fits

fabri is not a pure SDK and not a monolithic autonomous app — it's the **engine
both could be built on, plus a builder that scaffolds products on it.**

- **vs an embeddable SDK.** fabri is Python-first and polyglot, and it is
  **memory-loop-first** — agents get cheaper and more reliable per session. The
  builder *scaffolds* a product instead of making you hand-wire every agent,
  tool, and prompt.
- **vs an autonomous "does-everything" app.** fabri is an **embeddable engine +
  builder you build _your_ product on**, then ship as a self-contained service —
  not a single fixed application. You own the product; fabri is the base.

The throughline: **idea → scaffold the agent and tools → run**, on an engine that
gets cheaper every session.
