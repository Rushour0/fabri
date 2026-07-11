# 04 · Sandboxing — from path-jail to real container isolation

**What you'll learn:** fabri's layered, opt-in isolation model, the security
posture of the Docker backend, and how to wire it programmatically.

**Optimization methodology demonstrated:** *bounded, disposable execution
contexts* — the isolation analog of delegation isolation (example 02). Untrusted
tool output runs in a context you can cap and throw away, so one bad step can't
escalate.

## The honest picture: isolation is layered and opt-in

fabri does **not** ship one all-or-nothing sandbox, and nothing is containerized
by default. Every tool call routes through a `Sandbox` object:

| Backend | What it isolates | On by default? |
|---|---|---|
| **`LocalSandbox`** | A path jail: every file/shell tool refuses paths outside `FABRI_SANDBOX_ROOT` (set from `tools.sandbox_root`). Plus a per-call timeout, a process-group kill, and a 1 MiB stdout cap. | ✅ Yes — this is what `fabri run` gives you |
| **`DockerSandbox`** | Each tool call runs via `docker exec` in a pooled container: `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 512`. Memory and network limits are opt-in. | ❌ No — wired programmatically |

> Common misconception to avoid: `fabri init` does **not** containerize the
> agent. Its scaffolded `docker-compose.yml` only runs Qdrant (the optional
> vector store). The agent and its tools run on the host under `LocalSandbox`
> unless you wire `DockerSandbox` yourself.

## Run the driver (LocalSandbox, works today)

```bash
pip install 'fabri[sqlite]'
export GEMINI_API_KEY=...
python examples/04-docker-sandbox/run_sandboxed.py
```

It prints the active backend and jail root, then runs a read-only task. The
point is the **wiring**: `run_sandboxed.py` builds the tool registry, LLMs, and
memory store by hand (instead of `fabri run`) so it can choose the sandbox
backend — because that choice lives in code, not YAML.

## Upgrade to real container isolation

1. Build the base image from the repo root:
   ```bash
   docker build -f src/fabri/sandbox/Dockerfile.base -t fabri/sandbox:latest .
   ```
   It's a minimal `python:3.12-slim` with fabri installed and an unprivileged
   `fabri` user; `FABRI_SANDBOX_ROOT=/workspace` inside.

2. Uncomment the `DockerSandbox` block in `run_sandboxed.py`. The two lines that
   actually flip isolation on:
   ```python
   tools.sandbox = DockerSandbox(image="fabri/sandbox:latest",
                                 bind_mounts={os.getcwd(): "/workspace"},
                                 backend=DockerBackend(mem_limit="512m", network="none"))
   tools.sandbox_root = "/workspace"   # tools now jail to the container path
   ```

3. The security flags (`cap_drop_all`, `no_new_privileges`, `pids_limit`) are on
   by default; `mem_limit` and `network` are **off** by default (host defaults)
   so network-using tools like `fetch_url`/`web_search` keep working — set them
   when you want a pure-compute jail.

## The one real caveat (stated plainly)

A tool manifest resolves its script to an **absolute path**. For a builtin tool
to run *inside* the container, that path must exist there too. The base image
installs fabri precisely so the builtin tools resolve; for your **own** tools,
the intended pattern is to extend the base image and copy them in:

```dockerfile
FROM fabri/sandbox:latest
COPY tools/agent_tools/ /opt/project/tools/agent_tools/
```

That's why `DockerSandbox` is the framework's *integration seam*: the framework
ships the interface and the security defaults; a host service supplies the image
and (via `sync_in_hook`/`sync_out_hook`) any project-state ferrying. `dispose()`
the sandbox in a `finally` block — a leaked container outlives the agent.

## Related isolation you already get for free

- **`FABRI_HOME`** — every run's traces/logs/locks nest under `<FABRI_HOME>/.fabri/`.
  The service launcher gives each run its own `FABRI_HOME`, so concurrent runs
  never share state. Sub-agents intentionally inherit their parent's `FABRI_HOME`.
- **Per-child budgets** (`agent.subagent.max_steps` / `max_cost_usd`) and
  **`tools.max_parallel_spawns`** — see example 02 — bound spawned work so it
  can't run away even without containers.
