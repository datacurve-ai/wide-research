# Config schema (TOML)

A minimum viable job:

```toml
brief = "Short sentence describing the operation."
name = "snake_case_identifier"
title = "Human Readable Title"
target_count = 3
inputs = ["one", "two", "three"]

prompt_template = """
Your Jinja2-flavoured template with {{ input }}.
"""

[[output_schema]]
name = "thing"
type = "string"
title = "Thing"
description = "What this field is."
format = "free text is fine"
```

## Required top-level keys

| Field | Type | Notes |
| --- | --- | --- |
| `brief` | string | One-sentence summary of the op. |
| `name` | string | `snake_case` identifier — used in output paths. |
| `title` | string | Human-readable label. |
| `prompt_template` | string | Jinja2 template; `{{ input }}` is interpolated. May contain `<file>/abs/path</file>` tags. |
| `target_count` | integer | Must equal `len(inputs)` (guards against truncated pastes). |
| `inputs` | array of string | One subtask per element. |
| `output_schema` | array of table | Each entry: `{name, type, title, description, format?, required?}`. `type` ∈ `string`/`number`/`boolean`/`file`/`directory`. Field `name` must be `snake_case`. `required` defaults to `true`; set `false` to let the agent omit the field (missing file/dir paths are silently skipped during collection). |

## Optional keys + defaults

```toml
model = "openai/gpt-5.5"          # any model the Agents SDK can resolve
parallelism = 0                   # 0/unset = unbounded (all at once)
max_turns = 100                   # per subtask
timeout_seconds = 1800            # per subtask wall clock — NOT the whole batch
modal_app_name = "wide-research"
output_dir = ""                   # base for <ts>_<name>_<slug>/

# All host paths → sandbox paths mounted into every subtask.
[mount_files]
# "/host/path.json" = "/workspace/path.json"

[resources]                       # slim research defaults
cpu = 0.25
memory = 512
# gpu = "T4"                      # or "A10G", etc.

[image]                           # base sandbox image
base_image = "python:3.12-slim"
apt_packages = []
pip_packages = []
run_commands = []                 # arbitrary shell on top of base + apt + pip
# dockerfile = "./Dockerfile"     # overrides base_image when set
workdir = "/workspace"
enable_docker = false

secrets = []                      # Modal secret names to expose inside sandboxes
```

### About `timeout_seconds` and `max_turns`

Both are **per subtask**, not whole-batch. Each sandbox starts its own
clock the moment it's created. Example: `target_count = 100`,
`parallelism = 10`, `timeout_seconds = 600` means each of the 100 agents
gets up to 10 minutes of its own wall clock — the total batch runtime is
bounded by `ceil(100/10) × 600s = 6000s` in the worst case, not `600s`.

## Inputs: how data gets into the sandbox

Three mechanisms, low to high ceremony:

1. **Plain `inputs` strings.** Interpolated into `prompt_template` as
   `{{ input }}`. No files copied.
2. **`<file>/abs/path</file>` tags.** Put one inside `prompt_template` or
   `inputs` — the host mounts the path (file *or* directory) and rewrites
   the tag to the sandbox-side location.
3. **`mount_files`.** Per-job, same-for-every-sandbox mounts. Good for
   shared reference docs, runbooks, config.

See `docs/file-io.md` in the repo for the round-trip details.

## Outputs

The agent calls `submit(success=…, output={…}, error=…)`. Host-side
behaviour per field type:

- `string` / `number` / `boolean` → stored inline in `results.jsonl` +
  `results.csv`.
- `file` → agent sets the field to the absolute sandbox path of a file.
  Host pulls it to `<run>/subtasks/<NNNN>/<field><ext>` (ext copied from
  the sandbox basename).
- `directory` → agent sets the field to the absolute sandbox path of a
  directory. Host pulls the contents to `<run>/subtasks/<NNNN>/<field>/`
  (no wrapper dir — the sandbox basename is stripped).

`results.jsonl` and `results.csv` get the local paths after collection.

Final run layout:

```
<run>/
  results.jsonl
  results.csv
  logs/0000.log  …
  state/0000.json  …
  subtasks/
    0000/
      trajectory.jsonl       # streamed agent events: messages, tool calls + outputs, reasoning
      <file_field>.md
      <dir_field>/
    0001/
      …
```

Want a per-field zip? `zip -r summaries.zip <run>/subtasks/*/summary_file.md`.

## Guardrails

- Upload size: warn past 10 host paths or 50 MiB total per sandbox. Hard
  error if any single upload is over 500 MiB.
- Timeouts are enforced twice: host-side via `asyncio.wait_for`, and
  Modal-side via `ModalSandboxClientOptions.timeout`. Whichever fires first
  stops billing.
