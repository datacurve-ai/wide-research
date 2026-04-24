---
name: wide-research
description: Fan out one OpenAI agent per input across N parallel Modal sandboxes and aggregate structured results (plus returned files/folders). Use it whenever the task is "do the same operation to N independent items" — wide search, wide fix, wide extraction — with N ≥ 5.
compatibility: Requires the `wide-research` CLI on PATH (install via `uv tool install git+https://github.com/datacurve-ai/wide-research`), a configured `modal` token, and an `OPENAI_API_KEY`. Run `wide-research doctor` to verify.
---

# wide-research

Each subtask gets its own Modal sandbox and its own LLM agent loop. The agent
has shell + file-edit + web-search tools. Results come back as JSON/CSV plus
any files or folders the agent wrote.

Think `Pool.map()` for research agents.

## When to use

Invoke wide-research when **all three** are true:

1. ≥ 5 independent items to process, same operation per item.
2. Each item benefits from a real agent loop (multi-step reasoning, shell
   access, web lookup) — not just a single-shot LLM call.
3. Strong per-item isolation is useful (sandbox, own shell, own background
   jobs, own failure domain).

**Do NOT use for:**

- Tasks that need shared state across items → use one agent with a loop.
- Tasks solvable with a single LLM call → just call the LLM.
- Fewer than ~3 items → overhead dwarfs benefit.

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
≤ 50 MiB total per sandbox. wide-research warns past those and hard-errors on
any single upload over 500 MiB. Shipping big folders to every subtask burns
money and wall time.

## Setup (once per machine)

```bash
uv tool install git+https://github.com/datacurve-ai/wide-research
wide-research doctor            # verify token + key + paths
```

If `doctor` flags missing pieces:

```bash
modal token set --token-id ... --token-secret ...         # Modal
mkdir -p ~/.wide-research
printf 'OPENAI_API_KEY=sk-...\n' > ~/.wide-research/.env  # OpenAI
chmod 600 ~/.wide-research/.env
```

Everything the tool owns lives under `~/.wide-research/` (override via
`WIDE_RESEARCH_HOME`). `.env` is auto-loaded from, in priority: `--env-file`,
CWD `.env`, `~/.wide-research/.env`, repo `.env` (source checkouts only).

## How to invoke

`wide-research run` is **async by default**: it spawns the job as a
detached worker, prints the run dir + log path + follow-up hints, and
exits immediately. Use `wr tail` / `wr wait` (or `tail -f` the log) to
watch progress. Pass `--foreground` / `-f` if you want it to block.

Three input shapes — file, stdin, inline string:

```bash
# 1) File on disk (typical)
wide-research run job.toml --sample 3     # smoke-test first
wide-research run job.toml                # full run (detaches, returns immediately)

# 2) Stdin (useful when you're generating TOML programmatically)
cat job.toml | wide-research run -

# 3) Inline string (best for short one-shot jobs that don't deserve a file)
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

# After `run` returns, follow progress:
wide-research tail ~/.wide-research/runs/<dir>   # watch what each subtask is doing
wide-research wait ~/.wide-research/runs/<dir>   # block + one-line summary (non-zero if any fail)
tail -f ~/.wide-research/runs/<dir>/worker.log   # low-level worker log (API calls etc.)

# Stop a run early (tears down every live Modal sandbox first):
wide-research stop ~/.wide-research/runs/<dir>

wide-research run job.toml --foreground          # block THIS terminal until done
wide-research list                               # past runs
```

## Config shape

Required keys: `brief`, `name`, `title`, `prompt_template`, `target_count`,
`inputs`, and an `output_schema` array-of-tables. Everything else has
defaults. See `references/CONFIG.md` for the full schema. Example with
mixed I/O (file input via `<file>` tag, shared folder via `mount_files`,
scalar + file + directory outputs):

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

# Slim default resources are plenty for most research tasks.
[resources]
cpu = 0.25
memory = 512

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

After the run, local layout is:

```
<run>/
  results.jsonl / results.csv   # scalar fields inline; file/dir fields → local paths
  worker.log                    # low-level worker log (API calls, aggregate)
  logs/ state/
  subtasks/
    0000/coverage_report.html
    0000/failure_logs/…
    0001/…
```

**Budgets are per subtask, not per batch.** `timeout_seconds` and
`max_turns` apply to each individual sandbox independently — each agent
gets its own clock, starting when its sandbox is created. If you have
`target_count = 100`, `parallelism = 10`, `timeout_seconds = 600`, the
worst-case batch runtime is roughly `ceil(100/10) × 600s`, not `600s`.

**Budgets are per subtask, not per batch.** `timeout_seconds` and
`max_turns` apply to each individual sandbox independently — each agent
gets its own clock, starting when its sandbox is created. If you have
`target_count = 100`, `parallelism = 10`, `timeout_seconds = 600`, the
worst-case batch runtime is roughly `ceil(100/10) × 600s`, not `600s`.

## Resource presets

Defaults are **slim, research-oriented**. Override the `[resources]` table
when you need more. Modal's own floor is 0.125 CPU / 128 MiB; values are
*requests* — containers can burst above when the worker has capacity.

| Preset | cpu | memory (MiB) | When to use |
| --- | --- | --- | --- |
| default (research) | 0.25 | 512 | web research, small scripts, light scraping |
| coding | 2.0 | 4096 | `pytest`, ruff, small builds, data processing |
| docker | 4.0 | 8192 + `enable_docker = true` | DinD, container builds |

See `references/PRESETS.md` for ready-to-paste TOML fragments.

## Workflow

1. `wide-research plan job.toml --index 0` — render one prompt, no sandbox.
2. `wide-research run job.toml --sample 3` — validate end-to-end on 3 inputs.
3. Iterate the prompt; re-run from the CLI. Every run gets a timestamped dir.
4. `wide-research run job.toml` — full N inputs, in parallel.
5. `wide-research inspect <run_dir> --failed-only` — triage failures.

## Further reading

- `references/CONFIG.md` — full TOML schema + defaults.
- `references/PRESETS.md` — image / resource starter blocks.
- `references/AGENT_TOOLS.md` — what tools the in-sandbox agent has and how
  to use them from a prompt.
