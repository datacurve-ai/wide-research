"""Per-subtask runner.

One call = one Modal sandbox = one subtask. The agent harness (Agent +
Runner) lives here on the host; the sandbox is pure compute. Tool calls flow
through `ModalSandboxClient` → `session.exec(...)` / `session.read(...)`.

The session is pre-created (not delegated to `SandboxRunConfig(client=,
options=)`) so it's still alive after `Runner.run` returns — we need that to
pull file/directory outputs back before cleaning up."""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import modal
from agents import RunConfig, Runner, function_tool
from agents.agent import StopAtTools
from agents.extensions.sandbox.modal import (
    ModalImageSelector,
    ModalSandboxClient,
    ModalSandboxClientOptions,
    ModalSandboxSelector,
)
from agents.sandbox import Manifest as SdkManifest
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.entries import LocalDir, LocalFile
from agents.tool import WebSearchTool

from wide_research.config import Resources


log = logging.getLogger("wide_research.runner")

# Modal timeout is hard; SDK image builder version defaults match ours.
_DIND_IMAGE_BUILDER_VERSION = "2025.06"
_DOCKERD_READY_CMD = (
    "for i in $(seq 1 60); do "
    "docker info >/dev/null 2>&1 && exit 0; sleep 1; "
    "done; exit 1"
)


# ── Public data shapes ──────────────────────────────────────────────────────


@dataclass(slots=True)
class SubtaskSpec:
    """Everything run_subtask needs to start one sandbox. Built by the
    orchestrator from (config, one input)."""

    index: int
    name: str
    title: str
    brief: str
    input: str
    prompt: str
    model: str
    max_turns: int
    timeout_seconds: int
    output_schema: list[dict[str, Any]]
    workdir: str
    upload_files: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class RunEnv:
    """Ambient environment for every subtask — shared across the fan-out."""

    app: modal.App
    image: modal.Image
    resources: Resources
    secret_names: list[str]
    env_passthrough: dict[str, str]
    app_name: str
    enable_docker: bool
    sandbox_timeout: int


class Submission(dict[str, Any]):
    """Result payload for one subtask. Keys: success, output, error, meta."""


StateWriter = Callable[[int, dict[str, Any]], None]


# ── Entry point ─────────────────────────────────────────────────────────────


async def run_subtask(
    spec: SubtaskSpec,
    *,
    env: RunEnv,
    log_path: Path,
    subtask_dir: Path,
    semaphore: asyncio.Semaphore | None = None,
    state_writer: StateWriter | None = None,
) -> Submission:
    """Run one subtask end-to-end. Never raises — failures land in the
    Submission as `success=False` with an `error` string.

    `subtask_dir` is where this subtask's file/directory outputs will land
    (typically `<run>/subtasks/<NNNN>/`)."""
    kwargs = dict(
        spec=spec, env=env, log_path=log_path,
        subtask_dir=subtask_dir, state_writer=state_writer,
    )
    if semaphore is None:
        return await _run(**kwargs)
    async with semaphore:
        return await _run(**kwargs)


# ── Core body ───────────────────────────────────────────────────────────────


async def _run(
    spec: SubtaskSpec,
    *,
    env: RunEnv,
    log_path: Path,
    subtask_dir: Path,
    state_writer: StateWriter | None,
) -> Submission:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    t_start = time.monotonic()

    with log_path.open("w", buffering=1) as logf:
        emit = _make_emitter(logf, spec.index)
        notify = _make_state_notifier(state_writer, spec.index)

        notify(status="starting", phase="creating_sandbox", wall_s=0.0)
        secrets = _collect_secrets(env.secret_names, env.env_passthrough)
        sdk_manifest = SdkManifest(
            root=spec.workdir,
            entries=_manifest_entries_from_uploads(spec.upload_files, spec.workdir),
        )

        precreated_sb: modal.Sandbox | None = None
        if env.enable_docker:
            precreated_sb = await _precreate_docker_sandbox(
                spec=spec, env=env, secrets=secrets, emit=emit,
            )

        client, options = _build_client(env=env, precreated=precreated_sb)
        emit("creating session")
        session = await client.create(manifest=sdk_manifest, options=options)

        try:
            notify(status="running", phase="agent_loop")
            sink: dict[str, Any] = {}
            agent = _build_agent(spec=spec, sink=sink, manifest=sdk_manifest)
            run_cfg = RunConfig(
                sandbox=SandboxRunConfig(session=session, manifest=sdk_manifest),
            )

            run_error = await _run_agent_loop(
                agent=agent,
                prompt=spec.prompt,
                run_cfg=run_cfg,
                max_turns=spec.max_turns,
                timeout_seconds=spec.timeout_seconds,
                emit=emit,
            )

            payload = _payload_from_sink(sink, run_error)

            if payload.get("success"):
                notify(status="running", phase="collecting_outputs")
                await _collect_outputs(
                    session=session,
                    payload=payload,
                    subtask_dir=subtask_dir,
                    output_schema=spec.output_schema,
                    emit=emit,
                )

            elapsed = time.monotonic() - t_start
            payload["meta"] = {"wall_seconds": elapsed, "runner_note": run_error}
            notify(
                status="completed" if payload.get("success") else "failed",
                phase="done",
                wall_s=elapsed,
            )
            return payload
        finally:
            await _cleanup_session(session=session, client=client, emit=emit)


# ── Modal client + session plumbing ─────────────────────────────────────────


def _build_client(
    *,
    env: RunEnv,
    precreated: modal.Sandbox | None,
) -> tuple[ModalSandboxClient, ModalSandboxClientOptions]:
    selector = ModalSandboxSelector.from_sandbox(precreated) if precreated else None
    client = ModalSandboxClient(
        image=ModalImageSelector.from_image(env.image),
        sandbox=selector,
    )
    options = ModalSandboxClientOptions(
        app_name=env.app_name,
        timeout=env.sandbox_timeout,
        image_builder_version=_DIND_IMAGE_BUILDER_VERSION if env.enable_docker else "2025.06",
    )
    return client, options


async def _precreate_docker_sandbox(
    *,
    spec: SubtaskSpec,
    env: RunEnv,
    secrets: list[modal.Secret],
    emit: Callable[[str], None],
) -> modal.Sandbox:
    """Create a DinD sandbox ourselves; the SDK's ModalSandboxClient.create
    path doesn't pass `experimental_options` through, so we hand it a
    pre-built sandbox via `ModalSandboxSelector.from_sandbox(sb)`."""
    emit("creating DinD sandbox")
    sb = await asyncio.to_thread(
        modal.Sandbox.create,
        "/start-dockerd.sh",
        app=env.app,
        image=env.image,
        cpu=env.resources.cpu,
        memory=env.resources.memory,
        gpu=env.resources.gpu,
        secrets=secrets,
        timeout=env.sandbox_timeout,
        workdir=spec.workdir,
        experimental_options={"enable_docker": True},
    )
    emit(f"waiting for dockerd ({sb.object_id})")
    wait = await asyncio.to_thread(sb.exec, "bash", "-lc", _DOCKERD_READY_CMD, timeout=90)
    rc = await asyncio.to_thread(wait.wait)
    if rc != 0:
        emit("WARN: dockerd not ready after 60s — continuing anyway")
    else:
        emit("dockerd ready")
    return sb


async def _cleanup_session(
    *,
    session,
    client: ModalSandboxClient,
    emit: Callable[[str], None],
) -> None:
    try:
        await session.stop()
    except Exception as exc:  # noqa: BLE001
        emit(f"session.stop() failed: {exc}")
    try:
        await client.delete(session)
    except Exception as exc:  # noqa: BLE001
        emit(f"client.delete() failed: {exc}")


# ── Agent construction + loop ───────────────────────────────────────────────


def _build_agent(
    *,
    spec: SubtaskSpec,
    sink: dict[str, Any],
    manifest: SdkManifest,
) -> SandboxAgent:
    return SandboxAgent(
        name=f"{spec.name}/{spec.index}",
        instructions=_system_instructions(spec),
        model=spec.model,
        tools=[_make_submit_tool(sink), WebSearchTool()],
        tool_use_behavior=StopAtTools(stop_at_tool_names=["submit"]),
        default_manifest=manifest,
    )


async def _run_agent_loop(
    *,
    agent: SandboxAgent,
    prompt: str,
    run_cfg: RunConfig,
    max_turns: int,
    timeout_seconds: int,
    emit: Callable[[str], None],
) -> str | None:
    """Returns an error string on timeout/exception, None on clean exit."""
    try:
        await asyncio.wait_for(
            Runner.run(agent, prompt, run_config=run_cfg, max_turns=max_turns),
            timeout=timeout_seconds,
        )
        return None
    except asyncio.TimeoutError:
        msg = f"timeout after {timeout_seconds}s"
        emit(msg)
        return msg
    except Exception as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        emit(f"agent error: {msg}")
        return msg


def _payload_from_sink(sink: dict[str, Any], run_error: str | None) -> Submission:
    if sink:
        return Submission(
            success=bool(sink.get("success")),
            output=sink.get("output") or {},
            error=sink.get("error"),
        )
    return Submission(
        success=False,
        output={},
        error=run_error or "agent returned without calling submit()",
    )


def _system_instructions(spec: SubtaskSpec) -> str:
    """Describe the subtask, required outputs, and the submit contract.

    Deliberately does NOT teach the agent how to use its tools — the SDK
    already injects tool-specific instructions via the Shell / Filesystem
    capabilities, and the model knows WebSearchTool. Say what the job is
    and what the structured result must contain; trust the tool schemas
    for the rest."""
    lines = [
        f"You are subtask #{spec.index} of wide-research job {spec.name!r}.",
        f"Brief: {spec.brief}",
        "",
        "Call `submit(success=true, output={...})` with one key per output "
        "field listed below. On unrecoverable failure, call "
        "`submit(success=false, error='...')`. Calling submit ends the task.",
        "",
        "Output fields:",
    ]
    for f in spec.output_schema:
        lines.append(
            f"  - {f['name']} ({f['type']}): {f['title']} — {f['description']} "
            f"[format: {f['format']}]"
        )
    lines += [
        "",
        "For `file` / `directory` fields, write artefacts under "
        "/workspace/output/ and set the field value to the absolute sandbox "
        "path.",
        "",
        f"Budget: ≤ {spec.max_turns} turns, ≤ {spec.timeout_seconds}s wall clock.",
    ]
    return "\n".join(lines)


def _make_submit_tool(sink: dict[str, Any]):
    """Return a function_tool closed over `sink`. Calling it populates the
    sink and returns; StopAtTools ends the loop."""

    @function_tool(
        name_override="submit",
        description_override=(
            "Submit the final structured result for this subtask. Exactly one "
            "call ends the task. Pass success=true with a populated `output` "
            "dict, or success=false with a human-readable `error` string."
        ),
        strict_mode=False,
    )
    async def submit(
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str:
        sink["success"] = bool(success)
        sink["output"] = output or {}
        sink["error"] = error
        return "submitted"

    return submit


# ── SDK Manifest entries from upload map ────────────────────────────────────


def _manifest_entries_from_uploads(
    upload_files: dict[str, str],
    workdir: str,
) -> dict[str, Any]:
    """Convert `{host_path: sandbox_path}` into SDK Manifest entries.

    Entry keys are workspace-relative paths (relative to manifest.root).
    Missing sources are skipped with a warning."""
    root = workdir.rstrip("/")
    entries: dict[str, Any] = {}
    for host, sandbox in upload_files.items():
        src = Path(host)
        if not src.exists():
            log.warning("upload source missing: %s (skipping)", host)
            continue
        rel = _sandbox_to_rel(sandbox, root)
        entries[rel] = LocalDir(src=src.resolve()) if src.is_dir() else LocalFile(src=src.resolve())
    return entries


def _sandbox_to_rel(sandbox_path: str, root: str) -> str:
    if sandbox_path.startswith(root + "/"):
        return sandbox_path[len(root) + 1 :].lstrip("/")
    if sandbox_path.startswith("/"):
        # Absolute path outside the manifest root — stash under input/.
        return f"input/{posixpath.basename(sandbox_path.rstrip('/'))}"
    return sandbox_path.lstrip("/")


# ── Outputs ─────────────────────────────────────────────────────────────────
# For each `file` / `directory` field in output_schema, pull that single path
# out of the sandbox and land it at a predictable name under subtask_dir:
#
#   file       →  subtask_dir / <field_name><ext-from-source>
#   directory  →  subtask_dir / <field_name>/ (contents, not wrapped)
#
# The agent gets to choose where to write inside the sandbox (no /workspace/
# output/ convention required) — it just hands us an absolute path per field.


async def _collect_outputs(
    *,
    session,
    payload: Submission,
    subtask_dir: Path,
    output_schema: list[dict[str, Any]],
    emit: Callable[[str], None],
) -> None:
    outputs = dict(payload.get("output") or {})
    subtask_dir.mkdir(parents=True, exist_ok=True)
    for field_spec in output_schema:
        if field_spec["type"] not in ("file", "directory"):
            continue
        sb_path = outputs.get(field_spec["name"])
        if not sb_path:
            continue
        sb_path = str(sb_path)
        try:
            if field_spec["type"] == "file":
                local = await _pull_file(session, sb_path, subtask_dir, field_spec["name"])
            else:
                local = await _pull_dir(session, sb_path, subtask_dir, field_spec["name"])
            outputs[field_spec["name"]] = str(local)
            emit(f"pulled {field_spec['type']:<9} {field_spec['name']} ← {sb_path}")
        except Exception as exc:  # noqa: BLE001
            emit(f"WARN: failed to pull {field_spec['name']} from {sb_path}: {exc}")
    payload["output"] = outputs


async def _pull_file(session, sb_path: str, subtask_dir: Path, field_name: str) -> Path:
    """Pull one file from the sandbox. Local name is `<field_name><ext>`
    where ext is taken from the source basename (e.g. `.md`, `.json`)."""
    ext = Path(sb_path).suffix
    local = subtask_dir / f"{field_name}{ext}"
    reader = await session.read(Path(sb_path))
    try:
        data = reader.read()
    finally:
        _close(reader)
    if isinstance(data, str):
        data = data.encode()
    local.write_bytes(data)
    return local


async def _pull_dir(session, sb_path: str, subtask_dir: Path, field_name: str) -> Path:
    """Pull one directory tree from the sandbox. Contents land directly under
    `<field_name>/` (the sandbox basename is NOT included)."""
    local = subtask_dir / field_name
    local.mkdir(parents=True, exist_ok=True)
    # `tar -cf - -C <sb_path> .` tars the *contents* of sb_path to stdout.
    # No scratch file in the sandbox; the SDK hands us raw bytes.
    result = await session.exec(
        "tar", "-cf", "-", "-C", sb_path, ".",
        shell=False, timeout=120,
    )
    if result.exit_code != 0:
        raise RuntimeError(
            f"tar {sb_path!r} failed: {result.stderr.decode(errors='replace')[:500]}"
        )
    with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tf:
        tf.extractall(local)  # noqa: S202 — trusted source
    return local


def _close(reader) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


# ── Small helpers ───────────────────────────────────────────────────────────


def _collect_secrets(
    secret_names: list[str],
    env_passthrough: dict[str, str],
) -> list[modal.Secret]:
    secrets: list[modal.Secret] = [modal.Secret.from_name(n) for n in secret_names]
    if env_passthrough:
        secrets.append(modal.Secret.from_dict(env_passthrough))
    return secrets


def _make_emitter(logf, index: int) -> Callable[[str], None]:
    def emit(msg: str) -> None:
        logf.write(f"[{index}] {msg.rstrip()}\n")
    return emit


def _make_state_notifier(
    writer: StateWriter | None,
    index: int,
) -> Callable[..., None]:
    if writer is None:
        def _noop(**_: Any) -> None: ...
        return _noop

    def notify(**delta: Any) -> None:
        writer(index, delta)
    return notify
