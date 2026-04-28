# Agent design

How each subtask is wired, where the harness runs, and how the submit protocol
works.

## Layers

The harness (Agent + Runner) lives on the host. The sandbox is pure compute.
This is the pattern described in the OpenAI Agents SDK's sandbox docs: the
harness is the control plane around the model; compute is the execution plane
the model drives with tool calls.

```
┌─ host process (wide-research CLI) ─────────────────────────────────────┐
│                                                                        │
│  orchestrator.run(cfg, run_dir)                                        │
│    ├─ preflight: warn on >10 uploads or >50 MiB; fail on >500 MiB/path │
│    └─ for each subtask (bounded by cfg.parallelism):                   │
│         runner.run_subtask(spec, env, log_path, output_dir, ...)       │
│           ├─ image.build_image(spec.image) → modal.Image               │
│           ├─ build SDK Manifest(entries=…) from mount_files + <file>   │
│           ├─ ModalSandboxClient.create(manifest, options) → session    │
│           │     └─ Modal sandbox spins up; manifest entries            │
│           │        (LocalFile / LocalDir) get materialised into it     │
│           ├─ Runner.run(SandboxAgent, prompt, run_config=…)            │
│           │     └─ tool calls flow through the session:                │
│           │        exec_command / write_stdin / apply_patch / submit   │
│           ├─ session.read(...) for `file`-typed output fields          │
│           ├─ session.exec("tar -cf ...") + read for `directory` fields │
│           └─ session.stop() + client.delete(session)                   │
│                                                                        │
└────────────────────────────────────────────────────────────────────────┘
```

`orchestrator.py` also writes `<run>/state/<idx>.json` for each subtask so
`wr tail` (and any external watcher) can follow live progress: phase,
wall-clock seconds, success/failure.

## The agent loop

```python
from agents import Runner, RunConfig
from agents.sandbox import SandboxAgent, SandboxRunConfig, Manifest
from agents.extensions.sandbox.modal import (
    ModalSandboxClient, ModalSandboxClientOptions, ModalImageSelector,
)
from agents.tool import WebSearchTool

client  = ModalSandboxClient(image=ModalImageSelector.from_image(modal_image))
options = ModalSandboxClientOptions(app_name="wide-research", timeout=…)
manifest = Manifest(root="/workspace", entries={…})      # inputs as mounts

session = await client.create(manifest=manifest, options=options)   # pre-create

agent = SandboxAgent(
    name=..., instructions=..., model=...,
    tools=[submit_tool, WebSearchTool()],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["submit"]),
    default_manifest=manifest,
)
run_cfg = RunConfig(sandbox=SandboxRunConfig(session=session, manifest=manifest))

await asyncio.wait_for(
    Runner.run(agent, prompt, run_config=run_cfg, max_turns=cfg.max_turns),
    timeout=cfg.timeout_seconds,
)
```

Three independent things can end the loop:

| Terminator | How it's detected | Recorded as |
| --- | --- | --- |
| Agent calls `submit` | The tool writes into the host-side sink and returns; `StopAtTools` exits the Runner normally. | Payload from the sink. `success=True` or `False`. |
| Wall-clock `timeout_seconds` | `asyncio.wait_for` raises. `Modal`'s own sandbox timeout is a second line of defence. | Synthetic failure: `{success: False, error: "timeout after Ns"}`. |
| `max_turns` exhausted | Runner returns without the sink being populated. | Synthetic failure: `{success: False, error: "agent returned without calling submit()"}`. |

We **pre-create the session** (not relying on `SandboxRunConfig(client=, options=)`
to let the SDK create it) so the session is still alive after `Runner.run`
returns. That lets us pull file/directory outputs back via `session.read(...)`
before we stop the sandbox. Billing ends on `session.stop() + client.delete()`.

## Tools

Almost all tools come from the SDK:

| Tool | Source | Purpose |
| --- | --- | --- |
| `exec_command(cmd, tty, workdir, yield_time_ms)` | SDK `Shell` capability | Foreground + background shell. Start with `tty=true` and a short `yield_time_ms` → returns a `session_id` for later polling. |
| `write_stdin(session_id, chars, yield_time_ms)` | SDK `Shell` capability | Poll a running process (send `chars=""` to just wait) or send keys (`chars=""` for Ctrl-C). This is the "wait for background job" primitive. |
| `apply_patch(patch)` | SDK `Filesystem` capability | Write / create / delete files via a unified-diff-ish patch format. |
| `view_image(path)` | SDK `Filesystem` capability | Read images out of the sandbox. |
| `web_search` | `agents.tool.WebSearchTool()` | OpenAI-hosted web search. No sandbox involvement. |
| `submit(success, output, error)` | Ours | Structured completion. Populates a host-side sink; `StopAtTools` ends the loop. |

We don't implement a custom `shell` or `write_file` anymore — the SDK's
capabilities already route through the Modal session, so re-implementing them
would just be worse versions of the same thing.

## Inputs: the SDK Manifest

`mount_files` and `<file>` tags are converted into SDK manifest entries:

```
cfg.mount_files = {
    "/host/path/to/input.txt": "/workspace/input/input.txt",
    "/host/path/to/project":   "/workspace/input/project",
}
```

becomes

```python
Manifest(
    root="/workspace",
    entries={
        "input/input.txt": LocalFile(src=Path("/host/path/to/input.txt")),
        "input/project":   LocalDir(src=Path("/host/path/to/project")),
    },
)
```

The SDK materialises these into the sandbox during session startup — no custom
tar-and-extract on our side.

## Outputs

`file` and `directory` fields travel back after the agent submits. One
pull per field, landing at a predictable name under the subtask's dir:

- **`file`** → `session.read(sb_path)` streams the bytes directly. Written
  to `<run>/subtasks/<NNNN>/<field_name><ext>` (ext copied from the
  sandbox basename, e.g. `.md`, `.json`).
- **`directory`** → `session.exec("tar -cf - -C <sb_path> .", shell=False)`
  streams tar bytes straight to stdout; we extract them into
  `<run>/subtasks/<NNNN>/<field_name>/`. The sandbox basename is NOT kept
  as a wrapper — the contents land directly under the field name.

No scratch files in the sandbox, no random-hash filenames, no per-field
zip. `results.jsonl` / `results.csv` get the local paths rewritten in.

## Trajectories

The agent loop runs via `Runner.run_streamed(...)`. Every semantic event
the SDK emits — agent updates, message outputs, tool calls, tool outputs,
reasoning items, web-search calls — is appended as a single JSON line to:

```
<run>/subtasks/<NNNN>/trajectory.jsonl
```

The file is line-buffered so it streams during a long subtask (`tail -f`
works). Each row's `item` field is the underlying OpenAI Responses API
item dict (`item.to_input_item()`), so the file is the same shape the
model itself produced/consumed and is replayable as Responses input.

Token-level deltas (`raw_response_event`) are deliberately skipped; the
on-disk schema isn't stable and they balloon file size. The first row is
a `run_started` envelope (model, max_turns, timeout, prompt) and the
last row is `run_finished` (error or null + aggregated `Usage`).

## Docker-in-Docker

When `image.enable_docker: true`:

1. We pre-create a `modal.Sandbox` with `experimental_options={"enable_docker": True}`
   and `/start-dockerd.sh` as the entrypoint.
2. We wait up to 60s for `docker info` to succeed.
3. We wrap the live sandbox in `ModalSandboxSelector.from_sandbox(sb)` and
   hand it to `ModalSandboxClient(sandbox=…)`, so the SDK reuses our
   pre-built sandbox instead of creating a new one.

From the agent's perspective, `exec_command("docker run hello-world")` just
works.

DinD sandboxes default to at least 8 CPU cores and 16 GB memory (below that,
Docker operations starve). Set `resources.cpu` / `resources.memory` higher if
you expect big image builds.

## Why this shape

- **Harness on host.** Progress, pause, cancellation, and timeout decisions
  all live where they can act — in the process that owns the Modal sandbox
  handle. Killing an agent that's wedged on a shell call is a single
  `sb.terminate()` away, not a signal-through-a-sandbox dance.
- **Pre-created session, not client+options.** Lets us read file/directory
  outputs after `Runner.run` returns. Also makes cleanup explicit: our
  `finally:` always calls `session.stop() + client.delete(session)`.
- **Sink, not file, for the submit payload.** The agent runs on the host now,
  so a simple closure-captured dict is fine — no disk round-trip needed.
- **One sandbox per subtask, not a pool.** Clean isolation: a crash or
  timeout in one subtask can't touch its neighbours. We pay ~5 s per sandbox
  cold-start, which is acceptable for agent runs that already take tens of
  seconds to minutes.
