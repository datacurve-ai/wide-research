# File and directory I/O

wide-research sandboxes are isolated — they don't see your laptop's
filesystem, tokens, or installed tools. This page covers the three ways to
get data *in*, the one way to get results *out*, and the size limits you
should respect.

## The shape

```
┌──────────────┐     mounts (inputs)                   ┌─────────────────────┐
│     host     │  ──────────────────────────────────▶  │   Modal sandbox     │
│              │                                       │                     │
│              │     one session.read per file field   │   (agent writes     │
│              │     one tar-stream per dir field      │   wherever it wants)│
│              │  ◀──────────────────────────────────  │                     │
└──────────────┘                                       └─────────────────────┘
```

Inbound data is declared in the config and applied once per sandbox at
startup (by the Agents SDK from a `Manifest`). Outbound data is pulled
host-side — one fetch per `file`/`directory` output field — after the agent
calls `submit`, while the session is still alive.

---

## Inbound: 3 ways to land data

Use the smallest mechanism that works.

### 1. Plain strings in `inputs`

The item is interpolated into `prompt_template` as `{{ input }}`. Nothing
is copied — the text lives in the prompt.

```toml
prompt_template = "Research {{ input }}. Return its stock ticker."
inputs = ["Apple", "Microsoft", "Alphabet"]
```

Use this when the subtask only needs a word, sentence, or record. It's the
default shape.

### 2. `<file>` tags

A `<file>/host/abs/path</file>` tag anywhere in `prompt_template` (or in a
string inside `inputs`) does two things:

1. Mounts the referenced file *or directory* into the sandbox under
   `/workspace/input/<sha-prefix>_<basename>`.
2. Rewrites the tag to the sandbox-side path so the agent sees, e.g.,
   `/workspace/input/16755a61_paper-a.pdf`.

```toml
prompt_template = "Summarise the document at <file>{{ input }}</file>."
inputs = [
    "/home/me/docs/a.pdf",
    "/home/me/docs/b.pdf",
]
```

Use this when the specific file/directory changes per subtask.

### 3. `[mount_files]`

A host-to-sandbox path map applied to **every** sandbox:

```toml
[mount_files]
"/home/me/runbooks/triage.md" = "/workspace/RUNBOOK.md"
"/home/me/rules.json"         = "/workspace/rules.json"
```

Use this for material every subtask needs: a runbook, shared config,
reference schemas.

### Which one to use

| Case | Use |
| --- | --- |
| short text per subtask | plain `inputs` |
| different file/dir per subtask | `<file>` tags |
| same file/dir for every subtask | `mount_files` |

---

## Outbound: the submit protocol

The agent writes artefacts wherever it likes in the sandbox, then calls
`submit(output={…})` with one entry per field in `output_schema`.
wide-research then does one pull per file/directory field, each landing at
a predictable named spot.

| Field type | Agent sets the field to | Host does |
| --- | --- | --- |
| `string` / `number` / `boolean` | the value directly | stores in `results.jsonl` + `results.csv` |
| `file` | absolute sandbox path of a file the agent wrote | `session.read(path)` → writes bytes to `<run>/subtasks/<NNNN>/<field>.<ext>` (ext copied from the sandbox basename) |
| `directory` | absolute sandbox path of a directory | `tar -cf - -C <path> .` → extracts the *contents* into `<run>/subtasks/<NNNN>/<field>/` (no wrapper dir) |

After collection, the matching entry in `results.jsonl` is rewritten to the
local path. `results.csv` gets the local path in that column too.

**Run layout at the end:**

```
<run>/
  results.jsonl            # output[<field>] rewritten to local paths
  results.csv
  logs/0000.log  …
  state/0000.json  …
  subtasks/
    0000/
      <file_field>.md      # e.g. summary_file.md
      <dir_field>/         # e.g. annotated_project/ (its contents)
    0001/
      …
```

No random hashes. `<idx>` (zero-padded) identifies the subtask; the field
name identifies what's inside. Scales cleanly to thousands of subtasks.

---

## Size limits (and why)

wide-research fan-out works best when the per-sandbox footprint is small.
Every sandbox pays the upload cost independently — shipping a 2 GB folder
to 100 sandboxes burns 200 GB of transfer, not 2 GB.

| Limit | Enforcement |
| --- | --- |
| ≤ 10 host paths mounted per sandbox | warning |
| ≤ 50 MiB total mounted per sandbox | warning |
| ≤ 500 MiB for any single file/dir | **hard error** |

If you're bumping into these, consider:

- Filter the data host-side first. Do you really need the whole repo, or
  just the files that match a pattern?
- Stage once to shared cloud storage (S3, GCS, etc.) and have each agent
  `curl` the specific slice it needs.
- Use Modal `Volume` mounts directly (drop down from wide-research if the
  shape really needs that).

---

## What does *not* cross the boundary

- **No filesystem access** outside the mounts above. `~/.ssh`,
  `/etc/hosts`, your code checkouts — none of it is visible.
- **No credentials** from your shell, except `OPENAI_API_KEY` +
  `OPENAI_ORG_ID` / `OPENAI_PROJECT` (auto-passed so the agent can call
  OpenAI). Everything else has to come through `secrets` (Modal-managed)
  or `mount_files`.
- **No shared disk between subtasks.** Subtask 3's files are unreachable
  from subtask 4. That's the whole point of the fan-out.
- **No implicit output capture.** If the agent writes a file and doesn't
  return its path in `submit(output=...)`, it vanishes when the sandbox
  terminates.

---

## End-to-end example: directory round-trip

```toml
# Input: a project directory (via <file> tag).
# Output: an annotated copy (directory), plus scalar stats.

prompt_template = """
Copy every .py file under <file>{{ input }}</file> into
/workspace/output/project/, prepending a one-line docstring to each.
Write stats.json with the file count.

Then submit(success=true, output={
  "annotated_project": "/workspace/output/project",
  "py_files": <int>
}).
"""

[[output_schema]]
name = "annotated_project"
type = "directory"
title = "Annotated Project"
description = "The project with docstrings prepended."

[[output_schema]]
name = "py_files"
type = "number"
title = "Python File Count"
description = "How many .py files were processed."
format = "integer >= 0"
```

After the run, each subtask's annotated directory lives at
`<run>/subtasks/<NNNN>/annotated_project/` (its contents — the sandbox's
wrapper directory name is stripped), and `py_files` is a scalar in
`results.jsonl` / `results.csv`.

If you want one zip per output field, just `zip -r x.zip subtasks/*/<field>/*`
(or for files, `subtasks/*/<field>.<ext>`).
