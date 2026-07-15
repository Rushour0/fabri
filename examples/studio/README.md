# Fabri Studio

A small **conversational UI you can drop onto any fabri agency**. Submit a task,
watch the run stream in, and answer the manager's questions mid-run — all in the
browser. It's an example/template, not a product: ~1 React app + `fabri serve`,
meant to be copied and adapted.

![message classes: a manager bubble, light narrator lines, an activity chip, a cost footer, and a question card](./docs/studio.png)

## What it shows

Studio maps fabri's typed run events onto distinct message classes so a run
reads like a conversation, not a log:

| fabri event | Studio renders it as |
| --- | --- |
| the root agent's `final` | **the manager's message** — the primary, load-bearing bubble |
| `narration` | a light "what's happening now" line (the narrator) |
| `thought` | a collapsible reasoning card |
| `tool_started` / `spawn_subagent` | a light **sub-agent / tool activity** chip |
| `ask_user` | an **interactive question card** — the manager pauses and asks you |
| `usage` | a cost / COGS footer |
| `failed` / `incomplete` / `error` | a terminal status |

The `ask_user` round-trip is the interesting part: the manager blocks mid-run,
its question streams to the browser over SSE, and your answer flows back to
unblock it — see [How it works](#how-it-works).

## Quickstart

You need Python ≥3.11 (with fabri installed) and Node ≥18.

```bash
# 1. Install fabri with the sqlite memory backend (no Qdrant/Docker needed).
pip install -e ".[sqlite]"          # from the repo root

# 2. Start the backend: fabri's built-in HTTP/SSE service, pointed at an agency.
export ANTHROPIC_API_KEY=...        # or edit demo/agent.yaml for another provider
fabri serve --config examples/studio/demo/agent.yaml
#   → fabri serve listening on http://127.0.0.1:8080

# 3. In another terminal, start Studio.
cd examples/studio
npm install
npm run dev
#   → http://localhost:5173
```

Open <http://localhost:5173>, type something like *"plan a weekend trip"*, and
hit **Run**. The demo agency is configured to ask you a clarifying question
before it answers — you'll see the question card appear mid-run.

Pointing at a different `fabri serve` host/port? Set `FABRI_SERVE_URL`:

```bash
FABRI_SERVE_URL=http://127.0.0.1:9000 npm run dev
```

## Drop it onto your own agency

Studio is agency-agnostic — it only speaks the `fabri serve` HTTP API. To use
your own config, just point `fabri serve` at it:

```bash
fabri serve --config path/to/your/agent.yaml
```

To get the most out of the UI, your config should:

- **configure a `llm.narrator`** — the cheap-model progress stream (see
  [`demo/agent.yaml`](./demo/agent.yaml)); without it, the light narration lines
  simply don't appear.
- **enable the `ask_user` tool** if you want the mid-run question card.

## How it works

```
 browser (Vite :5173)                    fabri serve (:8080)
 ────────────────────                    ──────────────────────
 POST /runs ─────────────────────────▶   launches `fabri run` subprocess
 EventSource /runs/<id>/events ◀──────    tails the run's trace JSONL (SSE)
   … start, narration, thought,           each trace event → one SSE frame
     tool_started, ask_user, final …
 POST /runs/<id>/answer ─────────────▶    delivers the answer to the run's
                                          ask_user Unix socket, unblocking it
```

- **Transport** is fabri's existing `fabri serve` — Studio adds no backend of
  its own. The Vite dev server proxies `/runs` and `/health` to it so the
  browser stays same-origin (`fabri serve` sets no CORS headers).
- **Human-in-the-loop**: `fabri serve` runs a per-run listener on a Unix socket
  and points the agent's `ask_user` tool at it. When the agent asks a question,
  the listener writes an `ask_user` event into the run's trace (so it reaches the
  browser over the *same* SSE stream, carrying its `question_id`) and holds the
  socket open until `POST /runs/<id>/answer` supplies the answer.

## Layout

```
examples/studio/
  demo/agent.yaml        a self-demonstrating sqlite-backed agency
  src/
    lib/events.ts        the fabri event → message-class mapping (the core contract)
    lib/timeline.ts      builds the ordered, deduped conversation timeline
    hooks/useRunEvents.ts one run over one EventSource; ends on the `result` frame
    components/          Message, AskUserCard, Composer
    App.tsx
  vite.config.ts         dev proxy → fabri serve
```

## Scope

This is a deliberately small single-run example. It does **not** do multi-turn
chat, thread history, auth, or reload-mid-run recovery — see the ludexel app for
a fuller, product-grade take on the same idea.
