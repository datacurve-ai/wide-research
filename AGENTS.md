# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Copilot, etc.) working on
this repository.

## Repository overview

`wide-research` is a Python CLI + skill that spawns parallel subtasks in Modal
sandboxes. Each subtask runs an OpenAI Agent on one input from a shared TOML
prompt template; the host aggregates structured results plus returned files
or folders.

## Layout

```
.
├── pyproject.toml                    # package + CLI entry (Python ≥ 3.11)
├── README.md
├── AGENTS.md                         # this file
├── .env.example
├── docs/
│   ├── file-io.md                    # how files/dirs move in and out of sandboxes
│   └── agent-design.md               # harness/compute split, tool set, submit protocol
├── examples/
│   ├── echo_test.toml                # smallest-possible smoke test
│   ├── find_fortune_ceos.toml        # "wide search" example
│   ├── summarize_files.toml          # file-in / file-out
│   ├── annotate_projects.toml        # directory-in / directory-out
│   └── fixtures/                     # small sample inputs
├── skills/
│   └── wide-research/                # agentskills.io-compatible skill pack
│       ├── SKILL.md                  # main entry (small; triggers, when-to-use)
│       └── references/               # on-demand detail
│           ├── CONFIG.md             # full TOML schema
│           ├── PRESETS.md            # resource + image starter blocks
│           └── AGENT_TOOLS.md        # in-sandbox tool reference
├── src/wide_research/
│   ├── cli.py                        # typer CLI (doctor/run/plan/list/inspect/tail/wait)
│   ├── config.py                     # pydantic schema + TOML loader
│   ├── doctor.py                     # `wide-research doctor` probes
│   ├── prompt.py                     # Jinja2 + <file>…</file> tag resolution
│   ├── paths.py                      # run-dir resolution (env, --output)
│   ├── orchestrator.py               # fan-out + upload preflight + state files
│   ├── runner.py                     # per-subtask SDK-native agent loop
│   ├── image.py                      # modal.Image builder (+ DinD recipe)
│   ├── aggregate.py                  # results.jsonl + results.csv + zip bundles
│   └── runtime/
│       └── start_dockerd.sh          # DinD entrypoint when image.enable_docker=true
└── tests/
    └── smoke_modal.py                # non-OpenAI Modal plumbing smoke test
```

## Design docs

- [`docs/agent-design.md`](docs/agent-design.md) — harness on host, SDK Modal
  client, tool set, the three ways the loop terminates.
- [`docs/file-io.md`](docs/file-io.md) — the `<file>` tag, `mount_files`, and
  file/directory output semantics.

## Conventions

- **Python** ≥ 3.11 (for stdlib `tomllib`), type-hinted throughout, Pydantic v2.
- **Package manager**: `uv` (see README).
- **CLI**: Typer + Rich.
- **Configs**: TOML. Reader is stdlib `tomllib`; writer is `tomli_w` (used
  for the `config.resolved.toml` artifact).
- **Sandbox backend**: Modal via the Agents SDK's
  `agents.extensions.sandbox.modal.ModalSandboxClient`. We pre-create the
  session with `client.create(manifest=…, options=…)` so we can read
  outputs back after `Runner.run` returns; explicit teardown in `finally:`
  with `session.stop()` + `client.delete(session)`.
- **Agent SDK**: `openai-agents`. Harness lives on the host. `SandboxAgent`
  + `SandboxRunConfig(session=…)` route tool calls through the live Modal
  session. Default capabilities provide shell + filesystem tools. We add
  `WebSearchTool()` and our own `submit` (populates a host-side sink dict;
  `StopAtTools(['submit'])` ends the loop).
- **Resource defaults**: slim (0.5 CPU / 1024 MiB) to match Modal's
  request-based scheduling. Containers burst above the request when the
  worker has capacity — no hard floors of our own.
- **Secrets**: `.env` auto-loaded from `--env-file` → CWD `.env` →
  `~/.wide-research/.env` → shell env → repo `.env` (source installs).
  OpenAI credentials stay host-side; Modal `secrets = [...]` are explicit
  sandbox env vars only.

## Adding a skill (for this repo's skills/ dir)

Follow the [agentskills.io spec](https://agentskills.io/specification):

```
skills/
  {kebab-case-name}/
    SKILL.md          # required — frontmatter: name, description (single-line!), optional compatibility/license/metadata/allowed-tools
    references/       # optional — on-demand detail files, one level deep
    scripts/          # optional — bash scripts
    assets/           # optional — images, templates, etc.
```

Keep `SKILL.md` small (< 500 lines). Push detail into `references/` so
agents load it only when needed.

## Running the smoke tests locally

```bash
source .venv/bin/activate
wide-research doctor                                                     # credentials + paths
wide-research run  examples/echo_test.toml --dry-run                     # no Modal
python tests/smoke_modal.py                                              # Modal, no OpenAI
wide-research run  examples/echo_test.toml --sample 1                    # full path
```

## Common pitfalls

- `ModalSandboxClient` creates the session when given `client+options` alone
  and cleans it up on return — no chance to pull outputs. We pre-create via
  `client.create(manifest=, options=)` and pass `session=` on `SandboxRunConfig`
  so the SDK treats it as user-owned (`owns_session=False`) and skips
  cleanup. Our `finally:` then does `session.stop()` + `client.delete(...)`.
- `@function_tool` defaults to strict JSON schema mode. Tools that accept
  `dict[str, Any]` (like `submit`) must use `@function_tool(strict_mode=False)`.
- `StopAtTools(['submit'])` makes the Runner return *normally* when the
  named tool is called, but does NOT surface that tool's output as the final
  result. The tool has to mutate a host-side sink and we read from that
  sink.
- DinD can't go through `ModalSandboxClient.create` directly — it doesn't
  pass `experimental_options`. We pre-create a `modal.Sandbox` with
  `experimental_options={"enable_docker": True}` and hand it to the client
  via `ModalSandboxSelector.from_sandbox(sb)`. The SDK then reuses our
  sandbox instead of creating a new one.
- Modal image builds are content-addressed. Changing `pip_packages` /
  `apt_packages` / `run_commands` invalidates the cache; first run after a
  change is slow (~30s), subsequent sandboxes are ~1s to create.
- TOML doesn't serialize `None`. When writing the resolved config
  artifact we pass `exclude_none=True` to `model_dump()`.
