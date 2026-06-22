# fabri

Ever-evolving prompting and context engineering for LLM agents through
active memory and result analysis.

fabri is **not open source**, but it is **open for public use** as a
package on PyPI. You can install it, build agents with it, and rely on
the CLI and config surface. The internals and the direction of the
project are not open for contribution.

## Philosophy

An agent's prompt should not be written by hand and frozen. It should
grow from what the agent actually does.

```
                ┌──────────────────────────┐
                │      task arrives        │
                └────────────┬─────────────┘
                             │
                             ▼
        ┌───────────────────────────────────────────┐
        │  retrieve relevant guidelines from memory │
        │  (top-k by similarity, plus tool-tagged   │
        │   hits guaranteed when a tool is named)   │
        └────────────────────┬──────────────────────┘
                             │ injected into system prompt
                             ▼
                    ┌────────────────┐
                    │  agent loop    │ ── tool calls ──▶ subprocess tools
                    │  (ReAct)       │ ◀── results ────
                    └────────┬───────┘
                             │ JSONL trace
                             ▼
        ┌───────────────────────────────────────────┐
        │  analyze trace: compress each failure     │
        │  into a short, generalized guideline      │
        └────────────────────┬──────────────────────┘
                             │
                             ▼
        ┌───────────────────────────────────────────┐
        │  dedup vs existing tactical guidelines    │
        │  → near-duplicate? bump recurrence count  │
        │  → recurred across N sessions? promote    │
        │    from tactical to strategic             │
        └────────────────────┬──────────────────────┘
                             │
                             ▼
                  back into the memory store,
                  retrievable on the next task
```

A failure in session N becomes retrievable context in session N+1,
without anyone editing the prompt by hand. That loop — trace → analyze
→ compress → dedup → promote → retrieve — is the whole product.

Two operating principles fall out of that:

- **Context over prompt.** Keep retrieved context compact and
  just-in-time. Each tool gets one clear job. Tool results enter the
  context in a compact TOON encoding, not raw JSON.
- **Polyglot tools behind a uniform contract.** A tool is a JSON
  manifest next to an executable in any language. Stdin gets JSON args,
  stdout returns JSON, the runner normalizes errors. Agents can be
  composed as tools of other agents through the same contract.

## Install

```bash
pip install fabri                       # the `fabri` command lands on PATH
docker run -p 6333:6333 qdrant/qdrant   # vector store for memory
export ANTHROPIC_API_KEY=...
```

For OpenAI models: `pip install "fabri[openai]"` and set
`llm.provider: openai` in your config.

Embeddings run locally via `sentence-transformers/all-MiniLM-L6-v2` —
no embedding API calls.

## Quickstart

```bash
fabri init demo && cd demo
fabri --config agent.yaml run "greet Ada with the hello tool"
```

`fabri init` writes an `agent.yaml`, an example tool under
`tools/agent_tools/`, and a `docker-compose.yml`. You edit those, not
the library.

## Commands

```bash
fabri run "some task description"
fabri --config agent.yaml run "..."        # config-driven agent
fabri --verbose run "..."                  # DEBUG logging to console
fabri inspect-memory "a query"             # test retrieval
fabri ingest-traces <session-id>           # re-mine a past trace
```

Each `run` returns an outcome: `success`, `success_with_recovery`
(finished but a tool call failed along the way), or `incomplete` (hit
the step limit).

Every run writes two records keyed by `session_id`:

- `.fabri/traces/<session_id>.jsonl` — machine-readable trace used by
  the memory pipeline.
- `.fabri/logs/<session_id>.log` — always DEBUG-level, with LLM call
  latency/token usage, tool dispatch latency, and every dedup /
  promotion decision.

Both land under `.fabri/` in the directory you run from (override with
`$FABRI_HOME`). Add `.fabri/` to your project's `.gitignore`.

## Agents as tools

A `tools.agents` entry in `agent.yaml` exposes another agent as a tool
of this one. Each sub-agent is just another tool call in the parent's
normal loop. A sub-agent entry may carry `model` / `max_tokens`
overrides, so a parent on Sonnet can call a Haiku classifier without
duplicating the full config:

```yaml
tools:
  agents:
    - name: classify
      description: Classify a snippet into one of N labels.
      config: tools/agent_tools/classifier.yaml
      model: claude-haiku-4-5
      max_tokens: 256
```

See [`docs/creating-an-agent.md`](https://github.com/Rushour0/fabri/blob/main/docs/creating-an-agent.md)
for the full config surface and a worked example.

## License

[Apache-2.0](LICENSE) © Rushikesh Patade. Free to use. Not open for
contribution.
