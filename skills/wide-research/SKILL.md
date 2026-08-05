---
name: wide-research
description: Fan out one OpenAI agent per input across N parallel Modal sandboxes and aggregate structured results (plus returned files/folders). Use it whenever the task is "do the same operation to N independent items" — wide search, wide fix, wide extraction — with N ≥ 5.
compatibility: Requires the `wide-research` CLI on PATH (install via `uv tool install wide-research`), a configured `modal` token, and an `OPENAI_API_KEY`. Run `wide-research doctor` to verify.
---

# wide-research

Each subtask gets its own Modal sandbox and its own LLM agent loop (OpenAI
Agents SDK). The agent has shell + file-edit + web-search tools plus a
single `submit(...)` that ends the task. Results come back as JSON / CSV
rows plus any files or folders the agent wrote.

Think `Pool.map()` for research agents.

---

## When to use

Invoke wide-research when **all three** are true:

1. ≥ 5 independent items to process, same operation per item.
2. Each item benefits from a real agent loop (multi-step reasoning, shell
   access, web lookup) — not a single-shot LLM call.
3. Per-item isolation is useful (own sandbox, own shell, own background
   jobs, own failure domain).

**Do NOT use for:**

- Tasks that need shared state across items → use one agent with a loop.
- Tasks solvable with a single LLM call → just call the LLM.
- Fewer than ~3 items → overhead dwarfs benefit.

---

## Setup (once per machine)

```bash
uv tool install wide-research
wide-research doctor            # verify modal token + OPENAI_API_KEY + paths
```

Package: https://pypi.org/project/wide-research/

If `doctor` flags missing pieces:

```bash
modal token set --token-id ... --token-secret ...         # Modal
mkdir -p ~/.wide-research
printf 'OPENAI_API_KEY=sk-...\n' > ~/.wide-research/.env  # OpenAI
chmod 600 ~/.wide-research/.env
```

Everything the tool owns lives under `~/.wide-research/` (override via
`WIDE_RESEARCH_HOME`). `.env` loading priority (highest wins):
`--env-file` → CWD `.env` → `~/.wide-research/.env` → shell env → repo
`.env` (source checkouts). OpenAI credentials stay host-side; use TOML
`secrets = [...]` only for credentials the sandbox itself should read.

---

## Sandbox constraints — read this first

Subagents run in **fresh, isolated Modal sandboxes**. They do NOT have:

- your local filesystem
- cached logins, SSH keys, git credentials, cloud CLI auth, or tokens
- any tool or package installed on your machine
- access to internal networks

Anything they need has to arrive via one of:

- inline strings in `inputs` / `prompt_template`
- the `<file>…</file>` tag (mounts a host path into the sandbox)
- `mount_files` (mount the same host paths into every sandbox)
- `image` config (apt/pip packages baked into the sandbox image)
- `secrets` (Modal-managed secrets injected as env vars)

**Keep uploads small and few.** Aim for ≤ 10 host paths per sandbox and
≤ 50 MiB total per sandbox. wide-research warns past those and hard-errors
on any single upload over 500 MiB.

---

## How to invoke

`wide-research run` defaults to **spawn-detached + auto-tail**: it forks a
worker process and immediately attaches a live tail in your terminal.
Ctrl+C the tail → the worker keeps running in the background; wide-research
prints the exact command to stop it if you want to.

```bash
# Smoke-test first with a couple of inputs
wide-research run job.toml --sample 3

# Full run — spawns + tails (Ctrl+C the tail, worker keeps running)
wide-research run job.toml

# Block this terminal in-process (no worker child) — used by CI / scripts
wide-research run job.toml --foreground

# Spawn + exit immediately without tailing (pure detached)
wide-research run job.toml --detach

# Read config from stdin
cat job.toml | wide-research run -

# Inline config — handy for short one-shot jobs
wide-research run --inline "$(cat <<'EOF'
brief = "one-off smoke test"
name  = "inline_smoke"
title = "Inline Smoke"
target_count = 2
inputs = ["hi", "there"]
prompt_template = "Echo {{ input }}"

[[output_schema]]
name = "echo"
type = "string"
title = "Echo"
description = "echoed"
EOF
)"
```

Once detached, interact with a run via:

```bash
wide-research tail <run_dir>    # reattach tail
wide-research wait <run_dir>    # block until done; one-line summary; non-zero if any fail
wide-research stop <run_dir>    # graceful SIGTERM (tears down every live Modal sandbox)
wide-research list              # past runs under ~/.wide-research/runs/
wide-research inspect <run_dir> [--failed-only]  # compact table of results
```

---

## Config shape (TOML)

Minimum viable job:

```toml
brief = "Short sentence describing the operation."
name = "snake_case_identifier"
title = "Human Readable Title"
target_count = 3
inputs = ["one", "two", "three"]

prompt_template = """
Your Jinja2 template with {{ input }}.
"""

[[output_schema]]
name = "thing"
type = "string"
title = "Thing"
description = "What this field is."
format = "free text hint about the expected shape"
```

### Required top-level keys

| Field | Type | Notes |
| --- | --- | --- |
| `brief` | string | One-sentence summary of the op. |
| `name` | string | `snake_case` identifier — used in output paths. |
| `title` | string | Human-readable label. |
| `prompt_template` | string | Jinja2 template; `{{ input }}` is interpolated. May contain `<file>/abs/path</file>` tags. |
| `target_count` | integer | Must equal `len(inputs)` (guards against truncated pastes). |
| `inputs` | array of string | One subtask per element. |
| `output_schema` | array of table | Each entry: `{name, type, title, description, format?, required?}`. `type` ∈ `string`/`number`/`boolean`/`file`/`directory`. Field `name` must be `snake_case`. |

`required = false` on an `output_schema` entry lets the agent omit the
field. Missing file/directory paths are silently skipped (no host-side
pull). Missing scalar fields land as empty strings in `results.csv`.

### Optional top-level keys + defaults

```toml
model = "openai/gpt-5.6-sol"      # any model the Agents SDK can resolve
# reasoning_effort = "high"       # unset = model/provider default
parallelism = 0                   # 0/unset = unbounded (all at once)
max_turns = 100                   # per subtask
timeout_seconds = 1800            # per subtask wall clock — NOT the whole batch
modal_app_name = "wide-research"  # Modal groups sandboxes under this app name
output_dir = ""                   # base for <ts>_<name>_<slug>/

[mount_files]                     # shared files/dirs → every sandbox
# "/host/path.json" = "/workspace/path.json"

[resources]                       # slim research defaults
cpu = 0.25
memory = 512
# gpu = "T4"                      # or "A10G", "L4", "A100", "H100:8"…

[image]                           # base sandbox image
base_image = "python:3.12-slim"
apt_packages = []
pip_packages = []
run_commands = []
# dockerfile = "./Dockerfile"     # overrides base_image when set
workdir = "/workspace"
enable_docker = false             # one-click DinD; auto-installs docker.io

secrets = []                      # Modal secret NAMES to expose inside sandboxes
```

### About `timeout_seconds` and `max_turns`

Both are **per subtask**, not whole-batch. Each sandbox starts its own
clock when created. With `target_count = 100`, `parallelism = 10`,
`timeout_seconds = 600`, the worst-case batch runtime is
`ceil(100/10) × 600s = 6000s` — not 600s.

---

## Inputs: how data gets into the sandbox

Three mechanisms, low to high ceremony. Use the smallest one that works.

### 1. Plain `inputs` strings

Interpolated as `{{ input }}` in `prompt_template`. No files copied.

```toml
prompt_template = "Research {{ input }}. Return its stock ticker."
inputs = ["Apple", "Microsoft", "Alphabet"]
```

### 2. `<file>/abs/path</file>` tags

A `<file>` tag in `prompt_template` (or inside a string in `inputs`) does
two things:

1. Mounts the referenced file *or directory* into the sandbox under
   `/workspace/input/<sha-prefix>_<basename>`.
2. Rewrites the tag to the sandbox-side path (the agent sees a real
   absolute path like `/workspace/input/16755a61_paper-a.pdf`).

**Tags must contain an absolute host path** — wide-research does not
resolve relative paths.

```toml
prompt_template = "Summarise the document at <file>{{ input }}</file>."
inputs = [
    "/home/me/docs/a.pdf",
    "/home/me/docs/b.pdf",
]
```

### 3. `[mount_files]` — per-job, same-for-every-sandbox

```toml
[mount_files]
"/home/me/runbooks/triage.md" = "/workspace/RUNBOOK.md"
"/home/me/rules.json"         = "/workspace/rules.json"
```

Use this for material every subtask needs: runbook, shared config,
reference schema.

### Which one to use

| Case | Use |
| --- | --- |
| short text per subtask | plain `inputs` |
| different file/dir per subtask | `<file>` tags |
| same file/dir for every subtask | `mount_files` |

---

## Outputs: the submit protocol

The agent writes artefacts wherever it likes inside the sandbox, then
calls `submit(output={…})` with one entry per field in `output_schema`.
wide-research pulls file/directory fields back to a predictable spot.

| Field type | Agent sets the field to | Host does |
| --- | --- | --- |
| `string` / `number` / `boolean` | the value directly | stores in `results.jsonl` + `results.csv` |
| `file` | **absolute sandbox path** of a file the agent wrote | `session.read(path)` → writes bytes to `<run>/subtasks/<NNNN>/<field>.<ext>` (ext copied from the sandbox basename) |
| `directory` | **absolute sandbox path** of a directory | `tar -cf - -C <path> .` → extracts *contents* into `<run>/subtasks/<NNNN>/<field>/` (no wrapper dir) |

After collection, `output[<field>]` in `results.jsonl` is rewritten to the
local path. `results.csv` gets the local path in that column too.

**Always return absolute sandbox paths** — not host paths, not relative
paths. `/workspace/output/thing.md`, not `./thing.md` or
`/home/you/thing.md`.

### Run layout

```
<run_dir>/
  config.source.toml             # verbatim input
  config.resolved.toml           # after validation / defaults
  modal.json                     # app name, app_id, environment, dashboard URL
  results.jsonl                  # one line per subtask (file fields → local paths)
  results.csv                    # scalar view + local paths
  summary.json                   # totals: success/fail, wall time, cost, tokens
  worker.log                     # low-level worker log (API calls etc.)
  logs/0000.log …                # per-subtask live logs (what `wr tail` streams)
  state/0000.json …              # per-subtask status snapshots (phase, cost, etc.)
  subtasks/
    0000/
      trajectory.jsonl           # streamed message/tool/reasoning events (Responses API shape)
      <file_field>.md            # e.g. summary_file.md
      <dir_field>/               # e.g. annotated_project/ (its contents)
    0001/
      …
```

No random hashes in subtask output paths. `<idx>` (zero-padded) identifies
the subtask; the field name identifies what's inside. Scales cleanly to
thousands of subtasks.

### Want one zip per output field?

```bash
zip -r summaries.zip <run>/subtasks/*/summary_file.md
```

---

## What the in-sandbox agent can do

Fixed tool set per subtask:

- **`exec_command(cmd, workdir?, tty?, yield_time_ms?)`** — shell inside
  the sandbox. `tty=true` + short `yield_time_ms` starts a backgrounded
  process; `write_stdin(session_id, chars="")` polls / feeds input.
- **`apply_patch(patch)`** — add / update / delete files in one shot via a
  unified-diff-ish DSL. Prefer this over heredoc or `sed`.
- **`view_image(path)`** — inline an image (screenshots, plots).
- **`web_search(query)`** — OpenAI-hosted web search (runs off-sandbox).
- **`submit(success, output, error?, dangerously_bypass_required_fields?)`**
  — end the subtask. Missing required fields are rejected with a tool message
  so the agent can call `submit` again. Set the bypass flag only when a
  required field is intentionally impossible to provide.

Not provided: no `read_file` (use `exec_command("cat path")`), no
`list_dir` (`ls -la`), no HTTP fetcher (`curl` or `web_search`).

Agent prompts don't need to teach tool syntax — the SDK injects tool
schemas automatically. But do call out tools by name: "use `web_search`
to find …", "use `apply_patch` to write the file".

---

## Resources & image presets

Defaults are **slim, research-oriented** (0.25 CPU, 512 MiB). Override the
`[resources]` table when you need more. Modal's own floor is 0.125 CPU /
128 MiB; values are *requests* — containers can burst above when the
worker has capacity.

| Preset | cpu | memory (MiB) | When to use |
| --- | --- | --- | --- |
| default (research) | 0.25 | 512 | web research, small scripts, light scraping |
| coding | 2.0 | 4096 | `pytest`, ruff, small builds, data processing |
| docker (DinD) | 4.0 | 8192 + `enable_docker = true` | container builds, `docker run` inside the sandbox |
| GPU | 4.0 | 16384 + `gpu = "T4"` | inference, small training; pick the smallest GPU that fits |

`enable_docker = true` auto-installs `docker.io`, switches `iptables` to
legacy, and wires a dockerd entrypoint. For custom images, either set
`base_image = "python:3.13-slim"` (pulls from a registry) or
`dockerfile = "./Dockerfile"` (builds from a local Dockerfile).

See `references/PRESETS.md` for ready-to-paste `[resources]` + `[image]`
blocks including a headless-Chromium recipe.

---

## Cost tracking

Every run writes `summary.json` with per-job token + dollar totals from
the Agents SDK `Usage` object. Per-subtask cost lands in `state/<NNNN>.json`
too. Pricing is **best-effort**: an internal table keyed by model-name
prefix, overridable via the `WR_PRICE_TABLE_JSON` env var — a JSON object
of `{model: {input, cached_input?, output}}` in USD / million tokens.

If the model isn't priced, you still get token counts; the $ field is
`null` and the subtask is counted as `unpriced` in the summary. Cost
tracking **never breaks a run** — it's intentionally fail-open.

```json
// summary.json excerpt
"cost": {
  "requests": 57,
  "input_tokens": 312450,
  "cached_input_tokens": 128004,
  "output_tokens": 41209,
  "total_tokens": 353659,
  "cost_usd": 0.9812,
  "priced_subtasks": 12,
  "unpriced_subtasks": 0,
  "unpriced_models": []
}
```

---

## Modal app naming & environment

Every run is tied to a Modal app (default `wide-research`). Override with
`modal_app_name` in the config to separate runs by project:

```toml
modal_app_name = "olympus-conversion"
```

After launch, wide-research writes `modal.json` to the run dir with:

- the app name + app_id
- the active Modal profile + environment (`modal config current`)
- a dashboard URL (`https://modal.com/id/<app_id>`) — click to see every
  sandbox for this run live.

The URL is also printed at the end of a foreground / auto-tail run.

---

## Workflow

1. `wide-research plan job.toml --index 0` — render one prompt, no sandbox.
2. `wide-research run job.toml --sample 3` — validate end-to-end on 3 inputs.
3. Iterate the prompt; re-run from the CLI. Every run gets a timestamped dir.
4. `wide-research run job.toml` — full N inputs, in parallel (detaches, tails).
5. `wide-research inspect <run_dir> --failed-only` — triage failures.

---

## End-to-end example

```toml
brief = "For each project, run the test suite and bundle the report + failing logs."
name = "collect_test_reports"
title = "Collect test reports"
target_count = 3
inputs = [
    "/home/me/projects/alpha",
    "/home/me/projects/beta",
    "/home/me/projects/gamma",
]

# Same runbook mounted into every sandbox (available at /workspace/RUNBOOK.md).
[mount_files]
"/home/me/runbooks/pytest.md" = "/workspace/RUNBOOK.md"

[resources]
cpu = 2.0
memory = 4096

[image]
apt_packages = ["git"]
pip_packages = ["pytest", "pytest-cov"]

prompt_template = """
The project is mounted at <file>{{ input }}</file> (that's the sandbox path).

Consult /workspace/RUNBOOK.md for project-specific flags, then:
  - Run the tests with pytest-cov and write the HTML report to
    /workspace/output/coverage.html.
  - Collect all failing-test stdout/stderr under /workspace/output/failures/
    (one file per failing test).
  - Tally pass/fail counts.

Submit:
  submit(success=true, output={
    "coverage_report": "/workspace/output/coverage.html",
    "failure_logs":    "/workspace/output/failures",
    "pass_count":      <int>,
    "fail_count":      <int>
  })
"""

[[output_schema]]
name = "coverage_report"
type = "file"
title = "Coverage Report"
description = "pytest-cov HTML report."

[[output_schema]]
name = "failure_logs"
type = "directory"
title = "Failure Logs"
description = "One stdout+stderr file per failing test."
required = false            # some projects have no failures — that's fine

[[output_schema]]
name = "pass_count"
type = "number"
title = "Pass count"
description = "Number of passing tests."
format = "integer >= 0"

[[output_schema]]
name = "fail_count"
type = "number"
title = "Fail count"
description = "Number of failing tests."
format = "integer >= 0"
```

---

## Further reading

- `references/CONFIG.md` — full TOML schema reference.
- `references/PRESETS.md` — image / resource starter blocks.
- `references/AGENT_TOOLS.md` — detailed notes on the in-sandbox tool set.
