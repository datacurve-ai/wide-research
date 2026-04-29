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

`wide-research doctor` will tell you what's missing. If you use a custom env
file, check it with `wide-research doctor --env-file PATH`. Short version:

- Modal: `modal token set --token-id … --token-secret …`
- `OPENAI_API_KEY` and optional `OPENAI_BASE_URL` — any of (first hit wins):
  1. `--env-file PATH`
  2. `./.env` (current directory)
  3. `~/.wide-research/.env` (recommended for global installs)
  4. shell environment
  5. repo `.env` (source checkouts only)

The OpenAI credential and base URL are used by the host-side Agent runner and
are not injected into sandboxes. Use TOML `secrets = [...]` only for
credentials that the sandbox itself should be able to read.

### Where things live

Everything the tool owns is under `~/.wide-research/`:

- `~/.wide-research/.env` — credentials
- `~/.wide-research/runs/` — one subdirectory per run

Override with `WIDE_RESEARCH_HOME=/somewhere/else` (whole base) or
`WIDE_RESEARCH_RUNS_DIR=/somewhere/else` (runs only).

## Quickstart

`wide-research run` defaults to **spawn-detached + auto-tail**: it starts a
worker process, then follows the live merged log in your terminal. Ctrl+C only
detaches the tail; the worker keeps running. Use `--detach` / `-d` to spawn and
exit immediately, or `--foreground` / `-f` to run in-process for scripts/CI.

```bash
# 1. Smallest smoke test (3 subtasks, ~15s)
wide-research run examples/echo_test.toml

# 2. Dry-run — validate a config without spawning sandboxes
wide-research run path/to/your.toml --dry-run

# 3. Render just the first prompt
wide-research plan path/to/your.toml --index 0

# 4. Smoke-test, then full run. Use -f / --foreground for scripts/CI:
wide-research run path/to/your.toml --sample 5 --foreground
wide-research run path/to/your.toml               # detaches, then auto-tails
wide-research run path/to/your.toml --detach      # pure detached; wr wait to sync

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

`wr run` prints the run dir + pid and auto-tails by default. For a run started
with `--detach`, or after Ctrl+C detaches the tail, follow along with:

```bash
wr tail ~/.wide-research/runs/<dir>        # watch what each subtask is doing
wr wait ~/.wide-research/runs/<dir>        # block silently; non-zero exit if any fail
tail -f ~/.wide-research/runs/<dir>/worker.log   # low-level worker log

# Stop a run early (tears down every live Modal sandbox first):
wr stop ~/.wide-research/runs/<dir>

# Or, if you'd rather have `run` block the current terminal:
wr run job.toml --foreground
```

## Cost Estimates

Run summaries include token usage and best-effort cost estimates. By default,
wide-research fetches the public OpenRouter model catalog from
`https://openrouter.ai/api/v1/models` and caches it under
`~/.wide-research/cache/` for a week. Set `WR_DISABLE_PRICE_FETCH=1` to avoid
that network request, or provide your own prices with `WR_PRICE_TABLE_JSON`.

## License

MIT. See [`LICENSE`](LICENSE).

## Layout

See [`AGENTS.md`](AGENTS.md).
