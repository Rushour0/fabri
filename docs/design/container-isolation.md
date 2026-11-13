# Container isolation for `fabri serve` runs

Status: design, not built. Written against `main` at 5ebcc86 and the unmerged
`feat/service-run-hooks` branch at 7ff2737. This replaces the first container design
(`work-orders-container.md`), which a Codex review rejected with the verdict "redesign before
implementation". Section 4 answers each finding of that review.

## 1. What exists today

`launch_run` (`src/fabri/service/launcher.py:528`) starts each run with `subprocess.Popen` under
the service's own OS user. The run executes model-chosen tool calls, and several agencies enable
`bash`, so a run can open `installs.db` (every tenant's tokens in plaintext), read the service's
memory, and read other runs' homes. The credential broker
(`src/fabri/service/credential_broker.py`) keeps tokens out of the run's process, which only helps
when the run cannot read what the service reads. The service knows this and refuses credentialed
runs unless `FABRI_ALLOW_SHARED_UID=1` (`service.py:_same_uid_refused`).

Three more facts shape the design:

- `tail_events` (`tailer.py:98`) calls `path.exists()` and `path.read_text()` on a path inside the
  run's home on every poll. It follows symlinks, and the run owns that directory.
- `append_trace_event` (`ask_user_listener.py:301`) opens the same path with `open("a")`, so the
  service can be made to append into a file the run chose.
- `_peer_allowed` (`credential_broker.py:1043`) returns True when the platform reports no peer pid.

## 2. Shape of the redesign

The first design mounted host directories into the container: the run home, a socket directory,
and "identity mounts" for configs. Seven of the eleven findings come from those mounts (path
overlap, symlinked traces, shared gid, socket peer identity on Docker Desktop, the env file, mount
visibility races). This design has no host mounts at all.

1. The service copies inputs in and outputs out as tar streams (`docker cp`). No host path is
   visible in the container, and the run's files live in a per-run Docker volume.
2. The service talks to the run over the container's own stdin and stdout, which the service
   holds as pipes because it started the container. One framed, multiplexed channel carries trace
   lines, broker requests, `ask_user` questions, the result envelope, and outbound network
   streams.
3. The container has `--network none`. Outbound connections go through the channel to an egress
   gateway inside the service, which applies a per-run policy.

The pipe is the identity. The service created container X and holds the only pipe to it, so every
frame on that pipe came from run X. No Unix socket crosses the host boundary, so peer credentials,
socket file modes, and socket bind mounts on macOS stop mattering for container runs.

```
fabri serve (service user)                      container (uid = per-run, --network none)
+--------------------------+  stdin/stdout    +---------------------------------------+
| ContainerLauncher        |  of `docker      | shim (python -I, fixed entrypoint)    |
|  MuxServer  <------------+--start -ai`------+-> MuxClient                           |
|   trace  -> TraceWriter  |                  |    /run/fabri/brk.sock  (broker)      |
|   broker -> CredentialBroker                |    /run/fabri/ask.sock  (ask_user)    |
|   ask    -> AskUserListener                 |    127.0.0.1:3128       (egress proxy)|
|   net    -> EgressGateway --> internet      |    tails its own trace file           |
|   result -> RunHandle    |                  |  child: python -I -m fabri.cli run    |
+--------------------------+                  +---------------------------------------+
   docker cp (tar in, tar out) by container id; volume fabri-run-<sid> at /work
```

The shim runs inside the container as the run's uid. The service treats everything it sends as
untrusted bytes from that run. A run that kills or replaces its shim can only speak the channel
protocol as itself.

## 3. Threat model and the guarantee

### 3.1 Attacker

The attacker is the run: the model's tool calls, `bash`, Python tool sources, sub-agents, and any
content in the staged workspace. It has arbitrary code execution inside its container as an
unprivileged uid with no capabilities. A second attacker is a tenant who controls the task text,
run overrides, and workspace files.

### 3.2 Guarantee claimed

Under `fabri serve --launch docker`, on a platform whose preflight passed, and assuming no
exploit of the Linux kernel, the container runtime, or the Docker daemon:

- G1, service secrets. A run cannot read `installs.db`, `auth.db`, `runs.db`, `index.jsonl`, the
  service's environment, the service's memory, or any other host file. No host path is mounted.
- G2, other runs. A run cannot read or write another run's files, trace, processes, sockets, or
  network streams. Each run has its own uid, volume, and namespaces, and shares no writable mount.
- G3, stored credentials. No stored token enters the container. The service attributes a broker
  request to a run by the pipe it arrived on. Another run's broker key is useless to a run that
  steals it.
- G4, host writes. The service writes run-supplied bytes only to files it created and opened
  itself, in a directory the run cannot reach.
- G5, network. The container has no interface except loopback. It cannot reach the service's HTTP
  port, other containers, the host, the LAN, or a cloud metadata address. Every outbound
  connection passes the service's egress policy for that run and is logged.
- G6, lifecycle. After cancel, `close()`, or a service crash followed by a restart, no container
  or volume of this service is left running.

### 3.3 Not claimed

- Docker daemon administrators are trusted. On Linux that is root and the `docker` group. On
  Docker Desktop it is every process of the logged-in Mac user. They can `docker exec` into a run
  and read its memory. The service user is one of them: it can drive the daemon, which on a
  rootful daemon equals root on the host.
- A kernel or runtime escape defeats everything here. Containers share the host kernel. Open
  question 5 covers a stronger runtime.
- Provider keys and `run_env_resolver` values are given to the run on purpose. The run can read
  them and send them to any destination its egress policy allows.
- Anything else the run is given (task text, staged inputs, broker responses) can leave through
  allowed egress.
- The trace, usage, cost, and result envelope are self-reported by the run. A hostile run can
  forge them. Billing that must be trusted needs metering outside the run.
- A run can use its own broker bindings for the tools its config declares. That is today's
  accepted limit and it does not change.
- Sub-agents and tool subprocesses inside one container share that run's identity.
- Denial of service is bounded by cgroup limits (memory, pids, cpu) only. The work volume has no
  size quota on Docker's local volume driver. Timing and resource side channels are out of scope.
- Process mode (`--launch process`) is unchanged and keeps the same-uid refusal.

## 4. Decisions, one per review finding

### 4.1 Staged inputs replace host mounts (Critical, mounts)

Decision: no `-v`, no `--mount type=bind`. The only mounts are one per-run named volume at `/work`
(`--mount type=volume,src=fabri-run-<sid>,dst=/work,volume-nocopy`) and two tmpfs mounts. The
service builds a tar of approved inputs and sends it with `docker cp - <cid>:/work` before the
container starts.

Reason: a copy cannot expose a sibling file later, cannot be redirected by a symlink after the
check, and needs no ownership agreement between host and container. It also works the same on
Docker Desktop, where file sharing remaps ownership.

Rules in `stage.py`:

- Sources come from the bound `run.yaml` only: the run home the service made, each real
  `tools.manifest_dir`, each `tools.agents[*].config`, and CA bundle files named in the service
  env. Operators add sources with `FABRI_RUN_STAGE=<host path>=<name>,...`.
- Overlap is checked in both directions after `realpath`. A source is refused when it is inside
  `home_root` (except this run's own home), when `home_root` is inside it, when it equals or
  contains the install, auth, or run databases, the service's virtualenv, or a Docker endpoint
  (`/var/run/docker.sock`, `/run/docker.sock`, `~/.docker/run`, the path in `DOCKER_HOST`, and the
  daemon's `DockerRootDir`).
- The walker opens each path component with `O_NOFOLLOW` relative to a directory descriptor. It
  copies regular files and directories only. Symlinks, hardlinked files (`st_nlink > 1`), sockets,
  FIFOs, and devices are refused, with the path named in the error.
- Destinations are computed by the service, never taken from config text. The allowlist is
  `/work/home` (the run home), `/work/in/<n>/...` (external sources, numbered), `/work/ws`,
  `/work/tmp`, `/work/out`. Two sources that map to one destination raise `StageError`.
- Tar members carry the run's uid and gid and mode `& 0o777`, so no setuid bit is staged.
- `run.yaml` is rewritten so host paths point at their staged copies. The broker binds from the
  service's host copy, so edits the run makes to its staged copy change nothing.
- There is no Docker socket, no Docker CLI in the image, and no path to add one: the mount list
  is a constant.

Outputs return the same way. After the container exits, the service runs
`docker cp <cid>:/work/home -` and `docker cp <cid>:/work/out -` and extracts into
`home_root/<sid>/export/`. The extractor accepts regular files and directories, rejects absolute
names, `..`, symlinks, hardlinks, and devices, and stops at a byte cap and a file-count cap.
`FileSyncHook.sync_out` receives the export directory.

One limit: Docker's local driver mounts a named volume `rw,relatime`, and the `-v` form rejects
`nosuid,nodev`. `/work` therefore lacks those flags. `--cap-drop ALL` removes `CAP_MKNOD` and
`no-new-privileges` makes setuid bits inert, and the stage tar carries neither. `/work` needs
`exec` anyway, because agents build and run code there.

### 4.2 Descriptor-safe traces (Critical, trace)

Decision: both of the review's options. The run's trace travels over the pipe, and the host trace
file is owned and opened by the service.

- The shim tails the trace file inside the container and sends each line as a `TRACE` frame. A
  symlink there reads only what the run can already read.
- The service accepts a line when it is at most 1 MiB and parses as a JSON object. It drops
  anything else and counts the drop. It appends accepted lines to
  `home_root/<sid>/.fabri/traces/<sid>.jsonl`, the path `run_trace_path` already returns, so
  replay of persisted runs needs no change. A run that exceeds the trace cap (256 MiB by default)
  is stopped.
- New `service/trace_store.py` owns every open of a trace file. `open_append` uses
  `O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW|O_CLOEXEC`, then `fstat`: regular file, owned by the
  service uid, `st_nlink == 1`. `open_read` does the same read-only. Each keeps one descriptor for
  its lifetime and never reopens by path.
- `tail_events` reads through `trace_store.TraceReader`, which keeps the descriptor and reads from
  its last offset. This also removes the full-file reread on every 50 ms poll.
- `append_trace_event` writes through `trace_store.open_append`, so an `ask_user` event can no
  longer follow a link.

In container mode the run home on the host is `0700`, owned by the service, and never mounted, so
the run cannot plant a link there. In process mode `O_NOFOLLOW` and `fstat` stop the symlink and
hardlink redirects, but a same-uid run still owns the directory. Process mode keeps its refusal.

### 4.3 Per-run identity (High, identity)

Decision: a unique uid and gid per live run, leased from a pool, passed as `--user U:U`. No shared
gid, no setgid directories, no ACLs.

Reason: with no shared host files the uid is defence in depth, and it costs one lease. It keeps
per-uid kernel accounting separate between runs, and it means a mistake that shares a volume
still meets a DAC wall.

`identity.UidPool` leases from `FABRI_RUN_UID_BASE` (default 20000) for `FABRI_RUN_UID_COUNT`
(default 20000) ids. The range sits under 65536 so it exists inside a rootless or remapped user
namespace. A lease is a `flock` on `home_root/.launch/uid/<uid>`, so two service processes on one
`home_root` never share a uid and a crashed service releases its leases. On a rootful Linux daemon
the preflight refuses a range that contains a uid from the host's passwd database. The pool size
is the cap on concurrent runs.

Shared writable mounts are impossible by construction: the mount list is one volume named after
the session id plus tmpfs. `verify_created` (4.7) asserts it on every container.

### 4.4 Environment and launch material (High, environment)

Decision: no env file, no `-e`, nothing secret on argv. The service sends the run's environment in
the first frame on stdin (`HELLO`). The shim sets it for the child process.

Reason: the review asked for a per-run file unlinked after create. The stdin frame is stronger:
nothing is written to disk, and `docker inspect` shows only the image's own defaults. A local test
confirmed `Config.Env` holds no launcher value. If a later change needs a file, it must follow the
review's rule: `0600`, outside every stage source, unlinked right after `docker create`.

- Containers run with `--log-driver none`. Docker's default `json-file` driver would otherwise
  write the run's stdout, which carries trace lines and broker requests, to a file on the host.
- `FABRI_CRED_SNAPSHOT=1` with `--launch docker` is a startup error. `submit` checks again and
  raises `BindError`. Any `FABRI_CRED_*` name other than the broker's own socket and key is refused
  in container mode, including one admitted by `FABRI_RUN_ENV_ALLOW`.
- `launcher.split_run_env` builds the container env from `build_child_env`. It drops host-only
  names (`PATH`, `HOME`, `TMPDIR`, `PYTHON*`, `VIRTUAL_ENV`, cache dirs, `FABRI_SANDBOX_ROOT*`,
  `GOOGLE_APPLICATION_CREDENTIALS`), sets container values (`FABRI_HOME=/work/home`,
  `HOME=/work/home/.home`, `TMPDIR=/work/tmp`, `PYTHONSAFEPATH=1`, `HTTPS_PROXY` and `HTTP_PROXY`
  pointing at the shim), and applies the caller's `env` last.
- Reserved names cannot be set by a caller or a resolver: `FABRI_HOME`, `FABRI_SESSION_ID`, the
  two socket variables, the broker key, the proxy variables, and `PYTHON*`. A reserved or
  never-allowed name raises `LaunchError`.
- The broker key still exists and `_handle_line` still checks it. Inside the container it
  separates the agent loop from tool subprocesses, which lose `FABRI_CRED_*`. Across runs the
  pipe decides.

### 4.5 Broker (High, broker)

Decision: container runs reach the broker over the channel. The service creates no host socket
for them. Credentialed runs are allowed on Docker Desktop with the claim in 3.3.

Reason: the review's choice was "refuse on Desktop, or narrow the claim to socket capability
authentication". That choice existed because Desktop relays socket connections and hides the peer.
With no socket there is no relayed peer. The run-to-run and run-to-service guarantees are the
same on Desktop and Linux. The Desktop narrowing is that every process of the Mac user is a
Docker admin, which 3.3 states.

- `CredentialBroker.bind(..., transport="channel")` registers the run without a socket and returns
  a binding whose `env()` names the in-container path `/run/fabri/brk.sock`.
- `CredentialBroker.handle_channel_request(session_id, line, client_gone)` runs the same
  `_handle_line` path. `session_id` comes from the launcher's own handle, never from the frame.
- In the container the shim owns `/run/fabri/brk.sock` (mode `0600`, tmpfs) and relays one request
  per stream. `credential_broker_client` does not change.
- Process mode fails closed: `_peer_allowed` returns False when `peer_uid` or `peer_pid` is None,
  on Linux and macOS. The "file mode and key remain" branch is deleted. Unknown platforms refuse.

### 4.6 Network (High, network)

Decision: `--network none`, plus an egress gateway in the service reached through the channel.

Reason: a private network per run was the other option. Docker's default address pools allow
about 30 bridge networks, which caps concurrency, and keeping the host unreachable from a bridge
needs root firewall rules. The channel is already authenticated per run, needs no Docker network
objects, and behaves the same on Desktop.

- The shim listens on `127.0.0.1:3128` and accepts HTTP `CONNECT` and absolute-form HTTP. Each
  connection becomes a `NET` stream on the channel. The run's env sets `HTTPS_PROXY`,
  `HTTP_PROXY`, and their lowercase forms, with `NO_PROXY` empty.
- `egress.EgressGateway.open(session_id, host, port)` resolves the name in the service, applies
  the run's `EgressPolicy`, and connects to the address it checked, reusing the pinning logic in
  `tools/security/ssrf.py`. It refuses loopback, private, link-local, and metadata addresses
  unless the operator listed that exact `host:port`.
- Policy modes: `public` (any public address on allowed ports, default 80 and 443) and
  `allowlist` (provider hosts from the bound config plus `FABRI_EGRESS_ALLOW`). Open question 1
  sets the default.
- A host-local service such as Qdrant is reached by an explicit allow entry. The launcher rewrites
  a loopback `QDRANT_URL` to `http://host.fabri.internal:<port>`, and the gateway maps that name to
  the service host's loopback for listed ports only.
- The gateway writes one audit line per connection: session, host, port, verdict, bytes.
- Limits: non-HTTP protocols that ignore proxy variables do not work. All egress bytes pass
  through Python and the Docker attach stream, which suits API traffic and is slow for multi-GB
  clones.

Preflight probes from inside a container built with the launch flags: only `lo` exists, and TCP
connects to `host.docker.internal`, the default gateway address, `169.254.169.254`, and the
service's own bind address all fail. Any success refuses docker mode.

### 4.7 Hardening (High, hardening)

Decision: every container is created with the flags below, then inspected before it starts.

```
docker create -i --init --log-driver none
  --name fabri-run-<sid12>-<rand6>
  --label fabri.home=<home id> --label fabri.boot=<boot id> --label fabri.session=<sid>
  --user U:U --read-only --cap-drop ALL
  --security-opt no-new-privileges --security-opt seccomp=<pinned profile>
  --network none --ipc private --cgroupns private
  --pids-limit 512 --memory 2g --memory-swap 2g --cpus 2 --ulimit nofile=4096:4096
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=512m,mode=1777
  --tmpfs /run/fabri:rw,nosuid,nodev,noexec,size=16m,uid=U,gid=U,mode=0700
  --mount type=volume,src=fabri-run-<sid>,dst=/work,volume-nocopy
  --workdir /work/ws
  --entrypoint /usr/local/bin/python <image id> -I -m fabri.service.container.shim
```

- Seccomp: the repo ships `service/container/seccomp.json`, a copy of Docker's default profile
  from a named moby commit with its sha256 recorded in the module. The launcher passes it on every
  create, so the daemon's default does not matter. The preflight probe reads `Seccomp:` in
  `/proc/self/status` and calls one syscall the profile denies. Docker mode refuses when the mode
  is not 2 or the call succeeds. `seccomp=unconfined` is never passed and no option enables it.
- PID: Docker has no `--pid private` value, and omitting the flag gives a private namespace.
  `verify_created` makes that explicit by asserting `PidMode == ""`.
- `spec.verify_created(inspect_json, spec)` runs after `docker create` and before `docker start`.
  It asserts: `Privileged` false, `PidMode` empty, `IpcMode` private, `NetworkMode` none,
  `ReadonlyRootfs` true, `CapDrop` is ALL, `CapAdd` empty, `Binds` empty, mounts equal to the
  expected three, `User` equal to the lease, the seccomp and `no-new-privileges` options present,
  `Image` equal to the pinned id, and the log driver `none`. A mismatch removes the container and
  raises `LaunchError`. This catches a daemon default or a CLI config that changes the result.
- AppArmor and SELinux keep the daemon's default profile. The launcher never passes an
  `unconfined` value for either.

### 4.8 Image (High, image)

Decision: launch by immutable image id, with a fixed isolated entrypoint.

- `FABRI_RUN_IMAGE` must be `repo@sha256:...` or a `sha256:` image id. A bare tag is refused
  unless `FABRI_RUN_IMAGE_ALLOW_TAG=1`, which is for local development. In every case the
  preflight resolves the reference once and each `docker create` uses the resolved id. A tag that
  moves later changes nothing until the next service start.
- Compatibility is checked by the channel handshake: the shim answers `HELLO` with its protocol
  version and fabri version, and a mismatch fails the launch. The image label is informational.
- The entrypoint and the child both run `python -I`. Isolated mode ignores `PYTHON*` variables and
  keeps the working directory and user site off `sys.path`, so a `fabri/` directory in the
  workspace cannot shadow the installed package. `PYTHONSAFEPATH=1` in the run's env covers
  `python -m` calls made by sub-agents.
- The root filesystem is read-only and root-owned, the run is not root, and no mount lands on
  `/usr`, `/usr/local`, or site-packages. The destination allowlist in 4.1 enforces the last part.
- `src/fabri/sandbox/Dockerfile.run` installs a built wheel (no editable install), `git`, and
  `ca-certificates`, and bakes in the MiniLM embedding model with `HF_HUB_OFFLINE=1` so a run
  needs no model download. `scripts/build_run_image.sh` prints the image id.

### 4.9 Lifecycle (High, lifecycle)

Decision: the service keeps the container id and performs every action on it. The attach CLI
process is a transport only.

Launch, in `ContainerLauncher.launch`: lease a uid, plan the stage, `docker volume create`,
`docker create`, `verify_created`, `docker cp` the stage tar, `Popen(docker start -ai <cid>)`,
start the mux, send `HELLO`, wait up to 20 s for `READY`. A failure at any step runs `_destroy`
synchronously (stop, remove container with `-v`, remove volume, release the lease) and raises
`LaunchError`, so `service._abandon_submit` has nothing container-side to clean.

- Completion: a waiter thread runs `docker wait <cid>`. Its exit code is the run's exit code. If
  the attach process exits while the container is alive, the run has lost its channel, so the
  waiter stops it with `docker stop`.
- Cancel: `ContainerRunHandle.terminate()` runs `docker stop -t <grace> <cid>`, then
  `docker kill` if `docker wait` has not returned, then the same teardown as a normal exit. It
  never asks whether the CLI process is alive.
- Teardown order: drain the pipes (4.11), export outputs, `docker rm -f -v <cid>`,
  `docker volume rm`, release the lease, close the trace writer, set `done`.
- `FabriService.close()` calls `launcher.close()`, which terminates every handle and joins each
  teardown thread with a deadline of grace plus 15 s. The threads are not daemon threads. It then
  lists `docker ps -aq --filter label=fabri.boot=<boot id>`, force-removes anything left, and
  logs survivors. `cmd_serve` calls `close()` on SIGTERM and SIGINT.
- Startup reaping: `home_root/.launch/home_id` is stable per home root. Each service process
  holds a `flock` on `home_root/.launch/boot/<boot id>.lock`. At start the launcher lists
  containers and volumes with this home's label and, for each boot id whose lock it can take,
  removes them. Containers of a live sibling process are left alone. That matters once
  `run_dispatcher` lets a worker process share a home root.

### 4.10 Preflight (Medium, preflight)

Decision: the full preflight runs at every service start on every platform. Nothing is cached on
disk.

It takes about two seconds. It checks: daemon reachable, `OSType` linux, image resolved to an id,
the hardening probe (uid, read-only root, capabilities empty, seccomp mode 2 with a denied
syscall, mount flags, memory and pids limits effective), the network probe in 4.6, a stage round
trip (`docker cp` in, shim echo over stdin and stdout, `docker cp` out), and both ends of the uid
range.

The in-memory result is keyed by daemon id, server version, image id, and the seccomp sha. The
launcher rereads `docker info --format '{{.ID}} {{.ServerVersion}}'` at most every 30 s and after
any failed Docker command. A changed key reruns the preflight before the next launch, and a
failure refuses new runs until it passes. Docker mode never falls back to process mode.

The socket bind-mount probe from the first design is gone because no socket is mounted.

### 4.11 Streaming (Medium, streaming)

Decision: dedicated threads per pipe, and the container's state decides completion.

- A stdout thread decodes frames. A stderr thread keeps a 64 KiB ring for the error envelope. A
  stdin thread writes from a bounded queue. Nothing calls `communicate()`, and no thread holds a
  lock while it blocks on a pipe.
- `NET` streams use a credit window per stream, so one slow download cannot stall trace or broker
  frames. Caps: 64 KiB per frame, 64 open streams per run, 8 MiB for the result envelope. A
  malformed or oversized frame stops the run.
- Stable EOF: after `docker wait` returns, the readers continue to EOF with a 5 s cap, then the
  service closes the pipes. `ContainerRunHandle.is_running()` stays True until the last trace
  line is on disk and `done` is set. `tail_events` then does its final read on a file that will
  not change, so there is no mount-visibility race.

## 5. Module and file plan

New package `src/fabri/service/container/`:

| file | contents |
|---|---|
| `mux.py` | Frame codec, `MuxServer` (service side), `MuxClient` (shim side), stream windows, limits. No Docker import. |
| `shim.py` | In-container entrypoint. Reads `HELLO`, serves the broker and ask sockets and the proxy port, spawns the agent, tails its trace, sends `RESULT` and `EXIT`. |
| `stage.py` | `StagePlan`, `plan_stage`, `build_stage_tar`, `rewrite_config_paths`, `extract_export`. |
| `spec.py` | `ContainerSpec`, pure `create_argv(spec)`, `verify_created(inspect_json, spec)`. |
| `docker_cli.py` | `DockerCLI`: `info`, `create`, `cp_in`, `cp_out`, `start_attached`, `wait`, `stop`, `kill`, `rm`, `inspect`, `ps`, volume calls. Runs the CLI with a minimal env (`PATH`, `HOME`, `DOCKER_*`). |
| `identity.py` | `UidPool.lease()`, `release()`, host passwd check. |
| `egress.py` | `EgressPolicy`, `EgressGateway.open`, audit line. |
| `preflight.py` | `run_preflight(docker, image) -> PreflightResult`, `PreflightError`. |
| `launcher.py` | `ContainerLauncher` (`launch`, `close`, `reap_orphans`), `ContainerRunHandle`. |
| `seccomp.json` | Pinned profile. |

Also new: `src/fabri/service/trace_store.py`, `src/fabri/sandbox/Dockerfile.run`,
`scripts/build_run_image.sh`.

Changes to existing files:

| file | change |
|---|---|
| `service/launcher.py` | `RunHandle` gains `wait_exit()` and `cleanup()`. New `ProcessLauncher` wraps `launch_run` behind the interface `ContainerLauncher` implements (`launch`, `isolation`, `close`). New `split_run_env` and `RESERVED_RUN_ENV`. `launch_run` keeps its behaviour. |
| `service/credential_broker.py` | `bind(transport=)`, `handle_channel_request`, `_peer_allowed` fails closed. Socket code is untouched for process mode. |
| `service/ask_user_listener.py` | Split `_handle_conn` into a socket reader and `handle_line(line, client_gone)`. `start(listen=False)` for channel mode. `append_trace_event` uses `trace_store`. |
| `service/tailer.py` | `tail_events` reads through `TraceReader`. Signature and termination rule unchanged. |
| `service/service.py` | `launch_mode` argument, `self._launcher`, `launch_isolation` taken from the launcher after preflight, snapshot refusal, `handle.wait_exit()` in `_watch_run_end`, `attach_run_pid` only for process handles, `sync_out` on the export directory, `close()` joins. |
| `cli.py` | `fabri serve --launch {process,docker}` (env `FABRI_LAUNCH`), `--preflight-only`, signal handlers that call `close()`. |
| `docs/connections.md` | Limits section rewritten from section 3 of this document. |

### 5.1 Compatibility with `feat/service-run-hooks` (7ff2737)

- `run_env_resolver` merges its result into `run_env` between the same-uid refusal and
  `launch_run`. The container launcher takes the same `env` argument, so resolver values arrive
  with no change to the hook. They travel in `HELLO` like every other value, so a resolver's
  provider key never reaches argv or `docker inspect`. A resolver that returns a reserved name
  (4.4), a `FABRI_CRED_*` value, or `FABRI_INSTALL_DB` gets `LaunchError`. The resolver's failure
  path already calls `_abandon_submit`, and the launcher's own cleanup keeps that sufficient.
- The resolver receives `session_id`, `catalog_ref`, `user_id`, `origin`, and `task`. No new field
  is added in this design.
- `run_dispatcher` and the public `run_and_deliver(overrides=)` still enter through
  `service.submit`, which is the only launch site. A worker process that builds its own
  `FabriService` on a shared home root is covered by the boot locks in 4.9 and the uid leases
  in 4.3.
- `_outcome_from_result` now reads `structured_output`, `usage`, `cost`, and `error`.
  `ContainerRunHandle.result()` returns the same envelope keys as `RunHandle.result()`, and a
  container that fails to start returns `success: False` with a string `error`, so
  `RunOutcome.error` is filled.
- Merge order: PRs 1 to 9 do not touch `service.py`, `surfaces/pipeline.py`, or
  `surfaces/types.py`. PR 10 edits `submit` and must be based on a `main` that already contains
  7ff2737.

## 6. Ordered PRs

Sizes are estimates of changed lines with tests. Every PR must pass the existing local suite.
Tests marked live need a Docker daemon and run under `pytest -m live_docker`.

1. Trace store and descriptor tailer (about 300 lines). `trace_store.py`, `tailer.py`,
   `append_trace_event`.
   Accept: a trace path that is a symlink to another file yields no events and raises
   `TraceOpenError`; the same for a hardlinked file; replacing the path with a symlink after open
   does not change what the reader returns; `append_trace_event` refuses a symlink and the target
   file is unchanged; existing tailer and `ask_user` tests pass; a 50 MiB trace tails in linear
   time.
2. Broker fails closed and gains the channel transport (about 300 lines).
   Accept: with `peer_uid` or `peer_pid` patched to None the connection is refused on Linux and
   macOS; `bind(transport="channel")` creates no socket file; a channel request for session A
   that carries session B's key is refused; a correct request resolves the token through
   `get_token` on that call; existing broker suites pass.
3. `AskUserListener` channel entry (about 150 lines).
   Accept: `handle_line` returns the same replies as the socket path for ask, answer, timeout, and
   client-gone; `start(listen=False)` creates no socket.
4. Mux codec, server, and client (about 550 lines). Pure Python, no Docker.
   Accept: round trip of every frame type; a truncated, oversized, or unknown frame closes the
   channel with `MuxProtocolError`; the 65th stream is refused; a 64 MiB echo on one stream while
   another sends 10,000 small frames completes without deadlock using real OS pipes; the reader
   delivers every frame written before EOF.
5. Stage and export (about 500 lines).
   Accept: a source that is an ancestor of `home_root` is refused; a source inside `home_root`
   other than the run's home is refused; a source with a symlinked component, a hardlinked file, a
   FIFO, or a socket is refused; `/var/run` and the `DOCKER_HOST` path are refused; duplicate
   destinations raise; tar members carry the leased uid and no setuid bit; `rewrite_config_paths`
   leaves no host path in the staged config; `extract_export` rejects symlink, hardlink, device,
   absolute, and `..` members and stops at the byte and file caps.
6. Spec, Docker CLI wrapper, uid pool (about 400 lines).
   Accept: a golden test of `create_argv` shows no `-v`, no `--mount type=bind`, no `-e`, no
   `--env-file`, no `--privileged`, and each flag in 4.7; `verify_created` fails on each
   single-field deviation; 200 concurrent leases across two processes are unique; a killed process
   frees its leases.
7. Image, seccomp profile, preflight (about 450 lines plus the Dockerfile).
   Accept with a fake CLI: a tag without the allow flag is refused; the resolved id is what
   `create_argv` receives; a probe that reports seccomp mode 0, a writable root, a non-loopback
   interface, or a reachable host address each raises `PreflightError`; a changed daemon id
   reruns the preflight. Live: preflight passes on native Linux and on Docker Desktop, where
   `docker info` reports `seccomp,profile=unconfined`.
8. Shim, `ContainerLauncher`, `ContainerRunHandle` (about 700 lines).
   Accept, live, with a fake agent script: the service trace file is byte-identical to the trace
   written in the container; the result envelope matches; `docker inspect` contains neither the
   broker key nor a canary provider key; killing the attach CLI with SIGKILL still ends with the
   container and volume removed; `terminate()` on a run that ignores SIGTERM removes it within
   grace plus 5 s; a launch that fails at each step leaves no container, volume, or lease;
   `reap_orphans` removes a dead boot's containers and leaves a live sibling's.
9. Egress gateway and shim proxy (about 600 lines).
   Accept: unit tests of the policy for public, private, loopback, link-local, metadata, a
   rebinding name, and an allowlisted private `host:port`. Live: `curl` in the container reaches
   a public test server through the proxy; a direct connect without the proxy fails; a `CONNECT`
   to the service's port, to `169.254.169.254`, and to `10.0.0.1` is refused and audited.
10. Service and CLI wiring (about 350 lines). Based on `main` with 7ff2737 merged.
    Accept: `--launch docker` sets `launch_isolation` to `container` only after preflight; a
    credentialed run is accepted without `FABRI_ALLOW_SHARED_UID`; `FABRI_CRED_SNAPSHOT=1` with
    docker mode exits 1 at start; a `run_env_resolver` value reaches the run's env and is absent
    from `docker inspect`; a resolver that returns `FABRI_HOME` or `FABRI_CRED_X_Y` fails the
    submit and leaves no run home; `close()` returns with no labelled container left; process
    mode tests pass unchanged; surface results carry `structured_output`, `usage`, `cost`, and
    `error` from a container run.
11. Adversarial suite and docs (about 400 lines). All live. Run A is an attack script, run B is a
    victim with a canary file, a canary token in `installs.db`, and its own broker binding.
    Accept:
    - A and B report different uids, and A sees only its own processes in `/proc`.
    - A's search of its whole filesystem finds neither B's canary nor the token canary.
    - The test hands B's broker key to A. A's request with that key is refused, and the fake
      upstream API never receives B's token.
    - A replaces its trace with a symlink to `/etc/passwd` and to `../../installs.db`. The service
      trace holds no byte of either, and B's trace is unchanged.
    - A writes `out/x` as a symlink to `/etc/shadow`, as a hardlink, and as a 10 GiB sparse file.
      The export refuses each and no file outside `export/` changes.
    - A cannot connect to the service port, to B, or to the host by any address, and its proxy
      requests to private addresses are refused.
    - A fork-bombs and fills `/tmp`. B finishes with success.
    - A kills its shim. A is marked failed, and its container, volume, and uid lease are gone.
    - The service is killed with SIGKILL mid-run and restarted. No container or volume with this
      home's label survives.

Only PR 10 exposes the flag. PRs 1 and 2 are safe to ship alone and fix today's process mode.

## 7. Platform matrix

| | Linux, rootful daemon | Linux, rootless daemon | Docker Desktop, macOS |
|---|---|---|---|
| Docker mode | supported | supported after the PR 11 suite passes there | supported, development only |
| G1 to G6 against the run | yes | yes | yes, inside the Desktop VM |
| Who is a daemon admin | root, `docker` group, the service user | the service user | every process of the Mac user |
| Credentialed runs | allowed | allowed | allowed with the claim in 3.3 |
| Seccomp | pinned profile, verified | pinned profile, verified | pinned profile, verified (daemon default is unconfined) |
| Per-run uid | host uid in the pool range, checked against passwd | subuid of the service user | uid inside the VM |
| cgroup limits | enforced | needs cgroup v2 delegation, preflight refuses otherwise | enforced inside the VM |
| Broker identity | pipe | pipe | pipe |
| Process mode broker | fails closed without peer creds | same | fails closed without peer creds |
| Hosted or multi-tenant use | yes | yes | no |

Checked on this machine (Docker Desktop 27.5.1) with a throwaway container: `docker create -i`
followed by `docker cp -` into the volume and `docker start -ai` passes stdin and stdout bytes
unchanged; an explicit seccomp profile gives `Seccomp: 2` though the daemon reports unconfined;
`--network none` leaves only `lo`; `Config.Env` shows image defaults only; `mknod` fails; the
named volume mounts `rw,relatime`; the volume root is root-owned, so the stage tar must carry the
top-level directories with the run's uid. Native Linux and rootless are unverified until PR 7
and PR 11 run there.

## 8. Open questions for the owner

1. Default egress policy. `public` lets agents browse and fetch anything on the public internet,
   and a hostile run can send its data anywhere. `allowlist` stops that and breaks `fetch_url`
   and web search unless each crew lists hosts. Recommended default: `public` with private ranges
   refused, and `allowlist` set per catalog entry for hosted tenants.
2. Operator provider keys in tenant runs. A run can read `OPENAI_API_KEY` and the like from its
   env. Recommended default: hosted multi-tenant use requires `run_env_resolver` to supply a
   per-tenant or spend-capped key, and the service-env key stays for single-tenant installs. No
   code blocks this in v1. It is a launch rule.
3. Crews on `FABRI_CRED_SNAPSHOT`. Docker mode refuses snapshot mode, so a crew with a stdio MCP
   server that needs a stored credential cannot run there. Recommended default: refuse, and move
   those crews to the http MCP transport.
4. Hosted daemon. A rootful daemon makes the service user root-equivalent on the host. Rootless
   removes that and adds cgroup setup. Recommended default: rootful Docker on a single-purpose EC2
   host for the first hosted deploy, and rootless once its live suite passes.
5. Stronger runtime for untrusted tenants. runc shares the host kernel. gVisor (`runsc`) narrows
   that at some speed and compatibility cost. Recommended default: runc, with
   `FABRI_DOCKER_RUNTIME` passed through to `--runtime` so a hosted deploy can choose `runsc`
   without a code change.
6. When docker mode becomes required. Recommended default: `process` stays the default for one
   release. After that, `fabri serve` on a non-loopback bind refuses to start without
   `--launch docker` or an explicit opt-out.
