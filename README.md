# wide-research

Spawn parallel subtasks in Modal sandboxes — each running an OpenAI Agent on
one input from a shared prompt template — and aggregate structured results
(plus files or folders) back to your machine.

Think `Pool.map()` for research agents.

## Install

### CLI

```bash
uv tool install git+https://github.com/datacurve-ai/wide-research
wide-research --help         # or the short alias: wr --help
wide-research doctor         # check credentials + paths
```

`uv tool install` puts `wide-research` and `wr` on your PATH in an isolated
`uv`-managed environment. Upgrade with `uv tool install --reinstall …`;
uninstall with `uv tool uninstall wide-research`.

### As a skill

```bash
npx skills add datacurve-ai/wide-research
```

Skill layout follows [agentskills.io](https://agentskills.io/specification).
You still need the CLI installed (above).

### From source (for development)

```bash
git clone https://github.com/datacurve-ai/wide-research ~/code/wide-research
cd ~/code/wide-research
uv venv --python 3.12 && uv pip install -e .
source .venv/bin/activate
```

### Credentials

`wide-research doctor` will tell you what's missing. Short version:

- Modal: `modal token set --token-id … --token-secret …`
- `OPENAI_API_KEY` — any of (first hit wins):
  1. `--env-file PATH`
  2. `./.env` (current directory)
  3. `~/.wide-research/.env` (recommended for global installs)
  4. shell environment
  5. Modal secret referenced via `secrets = ["openai-api-key"]` in the TOML

### Where things live

Everything the tool owns is under `~/.wide-research/`:

- `~/.wide-research/.env` — credentials
- `~/.wide-research/runs/` — one subdirectory per run

Override with `WIDE_RESEARCH_HOME=/somewhere/else` (whole base) or
`WIDE_RESEARCH_RUNS_DIR=/somewhere/else` (runs only).

## Quickstart

`wide-research run` is **async by default** — it spawns a detached worker,
prints where to find the logs, and exits immediately. Use `wr wait` / `wr
tail` to follow. Pass `--foreground` / `-f` if you want it to block.

```bash
# 1. Smallest smoke test (3 subtasks, ~15s)
wide-research run examples/echo_test.toml
wide-research wait ~/.wide-research/runs/<dir>    # block for the summary

# 2. Dry-run — validate a config without spawning sandboxes
wide-research run path/to/your.toml --dry-run

# 3. Render just the first prompt
wide-research plan path/to/your.toml --index 0

# 4. Smoke-test, then full run. Use -f / --foreground if you want the
#    CLI to block in the current terminal:
wide-research run path/to/your.toml --sample 5 --foreground
wide-research run path/to/your.toml               # detaches; wr wait to sync

# 5. Look at past runs
wide-research list
wide-research inspect ~/.wide-research/runs/<dir>
wide-research inspect ~/.wide-research/runs/<dir> --failed-only
```

## Config shape

TOML. Six required top-level keys + an `output_schema` array-of-tables:

```toml
brief = "Find the current CEO of each company."
name = "find_ceos"
title = "Find CEOs"
target_count = 3
inputs = ["Apple", "Microsoft", "Alphabet"]

prompt_template = """
Research the current CEO of {{ input }}.
Use web_search to verify, then call submit(success=true, output={"ceo": "..."}).
"""

[[output_schema]]
name = "ceo"
type = "string"
title = "CEO"
description = "Verified name of the current CEO."
```

See [`skills/wide-research/references/CONFIG.md`](skills/wide-research/references/CONFIG.md)
for the full schema, and [`skills/wide-research/references/PRESETS.md`](skills/wide-research/references/PRESETS.md)
for ready-to-paste `[resources]` / `[image]` blocks (research / coding / DinD / GPU).

## Design docs

- [`docs/agent-design.md`](docs/agent-design.md) — harness/compute split,
  the SDK-native tool set, and how the loop terminates.
- [`docs/file-io.md`](docs/file-io.md) — how files and directories move in
  and out of sandboxes (`<file>` tags, `mount_files`, `file` / `directory`
  output fields, size limits).

## Inline / stdin configs

For short configs you'd rather not leave on disk:

```bash
# Inline string
wr run --inline "$(cat <<'EOF'
brief = "tiny inline smoke"
name = "inline_smoke"
title = "Inline Smoke"
prompt_template = "Echo {{ input }}"
target_count = 2
inputs = ["hi", "there"]

[[output_schema]]
name = "echo"
type = "string"
title = "Echo"
description = "echoed"
EOF
)"

# Stdin via '-'
cat my-config.toml | wr run -
wr plan - < my-config.toml
```

## Watching a run

`wr run` detaches by default and prints the run dir + pid + hints. Follow along with:

```bash
wr tail ~/.wide-research/runs/<dir>        # watch what each subtask is doing
wr wait ~/.wide-research/runs/<dir>        # block silently; non-zero exit if any fail
tail -f ~/.wide-research/runs/<dir>/worker.log   # low-level worker log

# Stop a run early (tears down every live Modal sandbox first):
wr stop ~/.wide-research/runs/<dir>

# Or, if you'd rather have `run` block the current terminal:
wr run job.toml --foreground
```

## Layout

See [`AGENTS.md`](AGENTS.md).
