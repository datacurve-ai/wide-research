# Agent tools

Every in-sandbox agent has this fixed tool set. Reference it from your
`prompt_template` — agents get the tool schemas automatically, but prompts
often do better if they call out specific tools by name.

## Shell

**`exec_command(cmd, workdir?, tty?, yield_time_ms?, …)`** — run a shell
command inside the sandbox.

- Foreground use: default flags are fine. Returns stdout/stderr + exit code.
- Background use: start with `tty=true` and a short `yield_time_ms` (e.g.
  `500`). The command keeps running; you get back a `session_id` to poll.

**`write_stdin(session_id, chars="", yield_time_ms?)`** — follow-up tool for
a running process.

- `chars=""` → just poll for new output, returning whatever accumulated.
- `chars="text\n"` → feed stdin to an interactive process.

This pair is how you "run a background job, then wait for it" — no separate
wait primitive needed.

## Filesystem

**`apply_patch(patch)`** — add / update / delete files in one shot via a
small patch DSL. Prefer this over heredoc / `sed` for anything non-trivial.
Syntax:

```
*** Begin Patch
*** Add File: path/to/new.py
+print("hello")
*** Update File: path/to/existing.py
@@ def foo():
-    return 1
+    return 2
*** Delete File: path/to/stale.py
*** End Patch
```

**`view_image(path)`** — inline an image from the sandbox (screenshots,
plots, etc.).

## Web

**`web_search(query)`** — OpenAI-hosted web search. Runs outside the
sandbox, no network config required. Use it freely.

## Submit (wide-research–specific)

**`submit(success, output, error?)`** — end the subtask with a typed result.

- `success=true, output={…}` — one key per field in `output_schema`.
- `success=false, error="reason"` — unrecoverable failure.

Calling `submit` terminates the agent loop. Call it exactly once.

## Intentionally not provided

- No `read_file` — use `exec_command("cat path")`.
- No `list_dir` — use `exec_command("ls -la path")`.
- No HTTP fetcher — use `web_search`, or `exec_command("curl …")`.
- No tool-level retries — the Agents SDK already retries model calls;
  tool errors come back to the model, which decides what to do.
