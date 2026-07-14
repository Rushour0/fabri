# Contributing to fabri

Thanks for taking the time. fabri is **open source** under the
[Apache License, 2.0](LICENSE) — read it, build on it, embed it, no
license terms to negotiate. The core is open to contribution, including
the agent loop, memory pipeline, orchestration, and config surface —
not just tools and docs.

> [!IMPORTANT]
> **Core changes (agent loop, memory pipeline, orchestration, config
> surface) need a design discussion before code.** Open an issue laying
> out the problem and your proposed approach; a maintainer will weigh in
> on direction before you invest in an implementation. This isn't
> gatekeeping for its own sake — the memory loop and step loop have
> non-obvious invariants (trace format, dedup semantics, sandbox
> boundaries) that a design pass catches before a PR does. PRs that skip
> straight to a core rewrite without prior discussion will be asked to
> open that discussion first, not closed outright.

| You want to…                                   | Do this                                  |
|------------------------------------------------|------------------------------------------|
| Report a bug or wrong behavior                 | [Open an issue](#reporting-bugs) with a repro |
| Report a security issue                        | [Disclose privately](#security) — not a public issue |
| Fix a typo, doc error, or broken example       | Small PR, no issue needed                |
| Build a new capability without touching core   | [Ship a **tool**](#the-real-extension-point-tools) — fastest path, no design discussion needed |
| Change the agent loop, memory, orchestration, or config | [Open a design issue first](#contributing-to-the-core), then PR |

---

## The real extension point: tools

fabri's whole extensibility story is the **tool contract**, by design.
A tool is a JSON manifest next to an executable in any language — no
framework code changes, no fork. You almost never need to touch `src/`.

```json
{
  "name": "hello",
  "description": "One sentence the LLM uses to decide when to call this.",
  "command": ["python3", "hello.py"],
  "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}},
  "output_schema": {"type": "object"},
  "timeout_s": 10
}
```

The executable reads one JSON object from stdin, prints one JSON object
to stdout, and signals success with its exit code. The runner normalizes
timeouts, nonzero exits, and malformed output into a uniform
`{ok, error?, result?, stderr?}`. See the
[Writing a tool](README.md#writing-a-tool) guide and the worked examples
under [`src/fabri/tools/examples/`](src/fabri/tools/examples) (Python, Go,
Rust, Node).

**If your tool touches the filesystem or runs code, respect the sandbox**
(see below). That's the one rule a tool PR will be held to.

---

## Contributing to the core

The agent loop, memory pipeline (trace → analyze → compress → dedup →
promote → retrieve), orchestration, and config surface are open to
contribution, with one rule: **design before code.**

1. **Open an issue first.** State the problem, not just the fix — what
   breaks or is missing today, with a repro or a concrete scenario.
   Sketch your proposed approach and how it interacts with the existing
   invariants (trace format, dedup/promotion semantics, per-role LLM
   routing, sandbox boundaries). A maintainer will confirm direction or
   redirect you before you write the PR.
2. **Small, reviewable diffs.** A core PR that touches one seam
   (e.g. the promotion threshold, one step-loop role, one memory
   backend) reviews fast. A PR that rewrites several seams at once
   doesn't — split it.
3. **Tests pin behavior, not implementation.** New core behavior needs a
   test that would fail if the behavior regressed, not one that
   snapshots internals.
4. **Docs move with the code.** A core change that shifts behavior
   described in `docs/` (`HOW_FABRI_WORKS.md`, `vision.md`,
   `using-fabri-well.md`) updates those files in the same PR.

This is slower than shipping a tool, by design — the core is what every
downstream product depends on, so a bad seam there costs everyone who's
built on it. Tools remain the fast, no-discussion-needed path for new
capability; use the core track only when the capability genuinely can't
be a tool.

---

## Dev setup

```bash
git clone https://github.com/Rushour0/fabri && cd fabri
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # add ,sqlite to skip Docker, ,openai for OpenAI models
```

Memory needs a vector store. Either run Qdrant:

```bash
docker run -p 6333:6333 qdrant/qdrant
```

…or use the in-process backend by installing `.[sqlite]` and setting
`memory.backend: sqlite` in your config.

One test compiles the Go example tool; install Go 1.22+ if you want the
full suite green:

```bash
cd src/fabri/tools/examples/example_go_tool && go build -o sum_tool . && cd -
```

---

## Tests & checks

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) is the
contract — match it locally before opening a PR:

```bash
pytest tests/ -q
```

- Tests that need **Docker** (the `DockerSandbox` integration test) skip
  automatically when the `docker` CLI is absent — the rest of the suite
  stays Docker-free via a mock backend.
- Tests that need **Qdrant** expect it on `localhost:6333` (CI starts it
  as a service); sqlite-backed tests need no daemon.
- New behavior needs a test. Mirror the style of the file you're
  touching — small, named for what they pin, Docker/network-free where
  possible.

---

## Sandboxing — the rule for any tool that touches the host

fabri runs LLM-chosen actions, including the by-design arbitrary-code
tools (`python_exec`, `bash`, and `spawn_subagent`'s whole child loop).
There are two isolation layers:

- **Path jail (default).** Every file/code tool resolves paths against
  `$FABRI_SANDBOX_ROOT` (set from `tools.sandbox_root`) and rejects
  anything that escapes it. This is the layer config-driven runs use
  today. It is a path jail, **not** kernel-level isolation —
  `python_exec` / `bash` still run as your user. **If you write a tool
  that opens, writes, or executes paths, enforce the same jail** — see
  [`read_file.py`](src/fabri/tools/examples/read_file.py) /
  [`python_exec.py`](src/fabri/tools/examples/python_exec.py) for the
  pattern. A tool that ignores the jail won't be merged.
- **Container isolation (`DockerSandbox`).** The seam in
  [`src/fabri/sandbox/`](src/fabri/sandbox) runs every tool subprocess
  inside a pooled `fabri/sandbox` container (`--cap-drop ALL`,
  `no-new-privileges`, pids cap) — the *actual* kernel-level boundary.
  It's wired programmatically by host services that pool containers and
  ferry state, not from `agent.yaml`. Build the image to exercise it:

  ```bash
  docker build -f src/fabri/sandbox/Dockerfile.base -t fabri/sandbox:latest .
  ```

Either way, `spawn_subagent` keeps its own recursion-depth fork-bomb
backstop (`FABRI_SUBAGENT_MAX_DEPTH`, default 5) on top.

---

## Reporting bugs

Open an issue with:

1. **What you ran** — the config (redact keys) and the task/command.
2. **What happened vs. what you expected** — include the `outcome`
   (`success` / `success_with_recovery` / `incomplete`).
3. **The trace** — `.fabri/logs/<session_id>.log` is always DEBUG-level
   and usually pinpoints it. The matching `.fabri/traces/<session_id>.jsonl`
   helps for loop/memory issues. Redact anything sensitive first.
4. **Versions** — `fabri` (`pip show fabri`), Python, and OS.

A minimal reproducer earns a much faster fix than a description.

## Security

The arbitrary-code tools make some classes of bug security-relevant
(sandbox escapes, path-jail bypasses, fork-bomb gaps, prompt-injection
paths that reach a tool). **Do not file these as public issues.** Email
the maintainer at **pataderushikesh@gmail.com** with a description and a
repro; you'll get an acknowledgement and a fix timeline.

---

## Pull request etiquette

- **One change per PR.** A doc fix and a tool are two PRs.
- **Discuss anything non-trivial first** via an issue — especially
  anything that smells like a direction change (it'll save you the work).
- **Tests pass and are included.** See [Tests & checks](#tests--checks).
- **Match the surrounding code** — comment density, naming, the dense
  "explain the *why*" docstring style this codebase uses.
- **Keep the diff surgical.** No drive-by reformatting, no dependency
  additions without a reason in the PR description.
- **Conventional, descriptive commit messages.** Look at `git log` for
  the house style.

## Licensing of contributions

By submitting a contribution you agree it is licensed under the project's
[Apache License, 2.0](LICENSE), and that you have the right to
contribute it.

Releases are maintainer-only — see [RELEASING.md](RELEASING.md).
