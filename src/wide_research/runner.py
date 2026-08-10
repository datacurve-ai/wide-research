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
import json
import logging
import os
import posixpath
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TextIO

import modal
from agents import (
    ModelRetryBackoffSettings,
    ModelRetrySettings,
    ModelSettings,
    RunConfig,
    Runner,
    function_tool,
    retry_policies,
)
from agents.agent import ToolsToFinalOutputResult
from openai.types.shared import Reasoning
from agents.extensions.sandbox.modal import (
    ModalImageSelector,
    ModalSandboxClient,
    ModalSandboxClientOptions,
    ModalSandboxSelector,
)
from agents.models.openai_provider import OpenAIProvider
from agents.sandbox import Manifest as SdkManifest
from agents.sandbox import SandboxAgent, SandboxRunConfig
from agents.sandbox.capabilities import Compaction, Shell
from agents.sandbox.entries import LocalDir, LocalFile
from agents.tool import WebSearchTool

from wide_research.config import Resources
from wide_research.cost import CostBreakdown, estimate as estimate_cost


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
    reasoning_effort: str | None = None


@dataclass(slots=True)
class RunEnv:
    """Ambient environment for every subtask — shared across the fan-out."""

    app: modal.App
    image: modal.Image
    resources: Resources
    secret_names: list[str]
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
        secrets = _collect_secrets(env.secret_names)
        sdk_manifest = SdkManifest(
            root=spec.workdir,
            entries=_manifest_entries_from_uploads(spec.upload_files, spec.workdir),
        )

        precreated_sb: modal.Sandbox | None = None
        try:
            if env.enable_docker:
                precreated_sb = await _precreate_docker_sandbox(
                    spec=spec, env=env, secrets=secrets, emit=emit,
                )

            client, options = _build_client(env=env, precreated=precreated_sb)
            emit("creating session")
            session = await asyncio.wait_for(
                asyncio.to_thread(
                    _create_session_blocking,
                    client,
                    sdk_manifest,
                    options,
                ),
                timeout=_SESSION_CREATE_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            if precreated_sb is not None:
                try:
                    await asyncio.to_thread(precreated_sb.terminate, wait=False)
                except Exception as cleanup_exc:  # noqa: BLE001
                    emit(f"precreated sandbox terminate failed during cancel: {cleanup_exc}")
            raise
        except Exception as exc:  # noqa: BLE001
            msg = f"sandbox_create_failed: {type(exc).__name__}: {exc}"
            emit(msg)
            if precreated_sb is not None:
                try:
                    await asyncio.to_thread(precreated_sb.terminate, wait=False)
                except Exception as cleanup_exc:  # noqa: BLE001
                    emit(f"precreated sandbox terminate failed: {cleanup_exc}")
            elapsed = time.monotonic() - t_start
            notify(status="failed", phase="sandbox_create_failed", wall_s=elapsed)
            return Submission(
                success=False,
                output={},
                error=msg,
                meta={"wall_seconds": elapsed, "runner_note": msg},
            )

        try:
            notify(status="running", phase="agent_loop")
            sink: dict[str, Any] = {}
            agent = _build_agent(spec=spec, sink=sink, manifest=sdk_manifest, session=session)
            run_cfg = _build_run_config(session=session, manifest=sdk_manifest)

            subtask_dir.mkdir(parents=True, exist_ok=True)
            trajectory_path = subtask_dir / "trajectory.jsonl"

            run_error, usage = await _run_agent_loop(
                agent=agent,
                prompt=spec.prompt,
                run_cfg=run_cfg,
                max_turns=spec.max_turns,
                timeout_seconds=spec.timeout_seconds,
                emit=emit,
                trajectory_path=trajectory_path,
                spec=spec,
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

            cost = estimate_cost(spec.model, usage) if usage is not None \
                else CostBreakdown(spec.model, 0, 0, 0, 0, 0, 0, None, False)
            if cost.cost_usd is not None:
                emit(
                    f"cost: ${cost.cost_usd:.4f} "
                    f"({cost.requests} req, {cost.total_tokens} tok)"
                )
            else:
                emit(
                    f"cost: ? — no price for {cost.model!r} "
                    f"({cost.requests} req, {cost.total_tokens} tok)"
                )

            elapsed = time.monotonic() - t_start
            previous_meta = payload.get("meta") or {}
            payload["meta"] = {
                **previous_meta,
                "wall_seconds": elapsed,
                "runner_note": run_error,
                "cost": cost.to_dict(),
            }
            notify(
                status="completed" if payload.get("success") else "failed",
                phase="done",
                wall_s=elapsed,
                cost_usd=cost.cost_usd,
                total_tokens=cost.total_tokens,
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


def _create_session_blocking(
    client: ModalSandboxClient,
    manifest: SdkManifest,
    options: ModalSandboxClientOptions,
):
    """Run ModalSandboxClient.create outside the main event loop.

    Some Modal/Agents SDK session creation failures block before yielding
    back to asyncio, which makes `asyncio.wait_for(client.create(...))`
    ineffective. Running the coroutine in a worker thread lets the main
    orchestrator enforce a startup timeout and continue other subtasks.
    """
    return asyncio.run(client.create(manifest=manifest, options=options))


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


_DEFAULT_RETRY = ModelRetrySettings(
    max_retries=4,
    backoff=ModelRetryBackoffSettings(
        initial_delay=2.0,
        max_delay=60.0,
        multiplier=2.0,
        jitter=True,
    ),
    policy=retry_policies.any(
        retry_policies.network_error(),          # socket/timeout errors
        retry_policies.provider_suggested(),     # OpenAI says retry
        retry_policies.retry_after(),            # honor Retry-After header
        retry_policies.http_status(               # generic transient 5xx / edge
            [408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524]
        ),
    ),
)

_SESSION_CREATE_TIMEOUT_SECONDS = 180.0


def _openrouter_model_settings(
    *,
    spec: SubtaskSpec,
    reasoning: Reasoning | None,
) -> ModelSettings:
    """OpenRouter Anthropic caching needs an explicit top-level cache_control.

    The Agents SDK already sends prompt_cache_key, which OpenRouter can use for
    sticky routing, but Anthropic-family models still require cache_control on
    the Responses path. session_id pins the provider for the whole subtask.
    """
    session_id = f"wr:{spec.name}:{spec.index}"
    extra_body: dict[str, Any] = {
        "cache_control": {"type": "ephemeral"},
        "session_id": session_id,
    }
    if spec.model.startswith("moonshotai/"):
        extra_body["provider"] = {
            "order": ["fireworks"],
            "allow_fallbacks": False,
        }
    return ModelSettings(
        retry=_DEFAULT_RETRY,
        reasoning=reasoning,
        extra_body=extra_body,
        extra_headers={"x-session-id": session_id},
    )


def _build_agent(
    *,
    spec: SubtaskSpec,
    sink: dict[str, Any],
    manifest: SdkManifest,
    session: Any,
) -> SandboxAgent:
    reasoning = (
        Reasoning.model_construct(effort=spec.reasoning_effort)
        if spec.reasoning_effort
        else None
    )
    openai_base_url = (os.environ.get("OPENAI_BASE_URL") or "").lower()
    on_openrouter = "openrouter.ai" in openai_base_url
    # OpenRouter may route a model across providers that reject its
    # openrouter:web_search extension. Keep requests provider-portable; jobs can
    # still perform bounded lookups with curl from the sandbox.
    tools: list[Any] = [
        _make_submit_tool(sink, spec.output_schema),
        _make_wait_for_tool(session),
    ]
    if not on_openrouter:
        tools.append(WebSearchTool())
    capabilities = None
    if on_openrouter and spec.model.startswith("moonshotai/"):
        # Kimi's OpenRouter fallback providers accept function tools but reject
        # the SDK's custom apply_patch tool. Kimi jobs use exec_command for
        # workspace writes, so retain shell + compaction and omit filesystem.
        capabilities = [Shell(), Compaction()]
    return SandboxAgent(
        name=f"{spec.name}/{spec.index}",
        instructions=_system_instructions(spec),
        model=spec.model,
        model_settings=(
            _openrouter_model_settings(spec=spec, reasoning=reasoning)
            if on_openrouter
            else ModelSettings(retry=_DEFAULT_RETRY, reasoning=reasoning)
        ),
        tools=tools,
        tool_use_behavior=_make_submit_tool_use_behavior(sink),
        default_manifest=manifest,
        **({"capabilities": capabilities} if capabilities is not None else {}),
    )


def _build_run_config(*, session: Any, manifest: SdkManifest) -> RunConfig:
    openai_base_url = (os.environ.get("OPENAI_BASE_URL") or "").strip()
    openai_api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    custom_openai_base = bool(openai_base_url and "api.openai.com" not in openai_base_url)
    run_cfg_kwargs: dict[str, Any] = {
        "sandbox": SandboxRunConfig(session=session, manifest=manifest),
        "tracing_disabled": custom_openai_base,
    }
    if custom_openai_base:
        run_cfg_kwargs["model_provider"] = OpenAIProvider(
            api_key=openai_api_key or None,
            base_url=openai_base_url,
            use_responses=True,
        )
    return RunConfig(**run_cfg_kwargs)


async def _run_agent_loop(
    *,
    agent: SandboxAgent,
    prompt: str,
    run_cfg: RunConfig,
    max_turns: int,
    timeout_seconds: int,
    emit: Callable[[str], None],
    trajectory_path: Path,
    spec: SubtaskSpec,
) -> tuple[str | None, Any]:
    """Return `(error_msg_or_None, usage_or_None)`.

    Streams the agent run via `Runner.run_streamed` and records every
    semantic event (run items, agent updates) to `trajectory_path` as
    JSON Lines. Each line is line-buffered so the file is tail-friendly
    while the subtask is in flight.

    `usage` is the Agents SDK `Usage` object aggregated by the context
    wrapper — captured even on timeout/cancellation so partial cost data
    still lands in the payload."""
    trajectory_path.parent.mkdir(parents=True, exist_ok=True)
    t_wall_0 = time.monotonic()
    streaming_result: Any = None
    run_error: str | None = None
    usage: Any = None

    with trajectory_path.open("w", buffering=1, encoding="utf-8") as tf:
        _jsonl(tf, {
            "t_ms": 0,
            "type": "run_started",
            "agent": agent.name,
            "model": spec.model,
            "subtask_index": spec.index,
            "subtask_name": spec.name,
            "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
            "input": prompt,
        })

        try:
            streaming_result = Runner.run_streamed(
                agent, prompt, run_config=run_cfg, max_turns=max_turns,
            )
            try:
                async with asyncio.timeout(timeout_seconds):
                    async for event in streaming_result.stream_events():
                        _record_event(tf, event, t_wall_0)
            except (asyncio.TimeoutError, TimeoutError):
                run_error = f"timeout after {timeout_seconds}s"
                emit(run_error)
                _try_cancel_streaming(streaming_result)
        except asyncio.CancelledError:
            _try_cancel_streaming(streaming_result)
            raise
        except Exception as exc:  # noqa: BLE001
            run_error = f"{type(exc).__name__}: {exc}"
            emit(f"agent error: {run_error}")

        usage = _extract_usage(streaming_result)

        _jsonl(tf, {
            "t_ms": int((time.monotonic() - t_wall_0) * 1000),
            "type": "run_finished",
            "error": run_error,
            "usage": _usage_to_dict(usage),
        })

    return run_error, usage


def _record_event(tf: TextIO, event: Any, t_wall_0: float) -> None:
    """Serialize one SDK stream event as a single JSONL row.

    The OpenAI Agents SDK emits three event kinds:
      - `agent_updated_stream_event` — a (sub)agent has become current.
      - `run_item_stream_event` — a new RunItem (message, tool call,
        tool output, reasoning, handoff…) has been produced.
      - `raw_response_event` — token-level deltas. These are noisy and
        aren't a stable on-disk shape, so we skip them.

    For run-item events we emit the underlying Responses API item
    (`item.to_input_item()`) — same dict shape the model produced/
    consumed — so the file is replayable and reads cleanly against the
    OpenAI Responses API schema."""
    t_ms = int((time.monotonic() - t_wall_0) * 1000)
    try:
        ev_type = getattr(event, "type", None)
        if ev_type == "agent_updated_stream_event":
            new_agent = getattr(event, "new_agent", None)
            _jsonl(tf, {
                "t_ms": t_ms,
                "type": "agent_updated",
                "agent": getattr(new_agent, "name", None),
            })
        elif ev_type == "run_item_stream_event":
            item = getattr(event, "item", None)
            raw: Any = None
            if item is not None:
                try:
                    raw = item.to_input_item()
                except Exception:  # noqa: BLE001
                    raw = _safe_dump(getattr(item, "raw_item", None))
            agent_obj = getattr(item, "agent", None) if item is not None else None
            _jsonl(tf, {
                "t_ms": t_ms,
                "type": "run_item",
                "name": getattr(event, "name", None),
                "item_type": getattr(item, "type", None) if item is not None else None,
                "agent": getattr(agent_obj, "name", None) if agent_obj is not None else None,
                "item": raw,
            })
        # raw_response_event intentionally skipped.
    except Exception as exc:  # noqa: BLE001
        # Never let trajectory recording break the run.
        _jsonl(tf, {
            "t_ms": t_ms,
            "type": "trace_error",
            "error": f"{type(exc).__name__}: {exc}",
        })


def _jsonl(tf: TextIO, obj: dict[str, Any]) -> None:
    tf.write(json.dumps(obj, default=str, ensure_ascii=False))
    tf.write("\n")


def _safe_dump(raw: Any) -> Any:
    """Best-effort conversion of a Responses API object to a JSON-friendly dict."""
    if raw is None or isinstance(raw, (str, int, float, bool, list, dict)):
        return raw
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_unset=True)
        except Exception:  # noqa: BLE001
            return str(raw)
    return str(raw)


def _usage_to_dict(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    dump = getattr(usage, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, Any] = {}
    for key in ("requests", "input_tokens", "output_tokens", "total_tokens"):
        val = getattr(usage, key, None)
        if val is not None:
            out[key] = val
    return out or None


def _try_cancel_streaming(result: Any) -> None:
    if result is None:
        return
    cancel = getattr(result, "cancel", None)
    if not callable(cancel):
        return
    try:
        cancel()
    except Exception:  # noqa: BLE001
        pass


def _extract_usage(result: Any) -> Any:
    """Pull the aggregate `Usage` off a (streaming or final) RunResult.
    Fail open to None."""
    try:
        ctx = getattr(result, "context_wrapper", None)
        return getattr(ctx, "usage", None) if ctx is not None else None
    except Exception:  # noqa: BLE001
        return None


def _payload_from_sink(sink: dict[str, Any], run_error: str | None) -> Submission:
    if sink:
        payload = Submission(
            success=bool(sink.get("success")),
            output=sink.get("output") or {},
            error=sink.get("error"),
        )
        if sink.get("required_fields_bypassed"):
            payload["meta"] = {
                "required_fields_bypassed": sink["required_fields_bypassed"],
            }
        return payload
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
        "Call `submit(success=true, output={...})` with one key per REQUIRED "
        "output field listed below. Optional fields may be omitted. On "
        "unrecoverable failure, call `submit(success=false, error='...')`. "
        "Calling submit ends the task once the payload is accepted.",
        "",
        "`submit` checks required fields at runtime. If it returns a message "
        "about missing fields, fix the output and call `submit` again. Only "
        "set `dangerously_bypass_required_fields=true` when a required field "
        "is intentionally impossible to provide.",
        "",
        "Output fields:",
    ]
    for f in spec.output_schema:
        opt = "" if f.get("required", True) else " [optional]"
        lines.append(
            f"  - {f['name']} ({f['type']}){opt}: {f['title']} — {f['description']} "
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


def _make_wait_for_tool(session: Any):
    """A host-side polling tool that blocks the agent until a shell
    predicate becomes true inside the sandbox (or times out).

    Works around the SDK's 30-second PTY yield cap on `exec_command` /
    `write_stdin`: this handler runs in the wide-research Python process
    and internally calls `session.exec(...)` every `poll_interval_s`,
    so the agent's single tool call can block for arbitrary durations
    without the sandbox-side session dying."""

    @function_tool(
        name_override="wait_for",
        description_override=(
            "Block up to timeout_s until a shell predicate exits 0 in the sandbox. "
            "Returns {status: 'ready' | 'timeout' | 'error', elapsed_s, exit_code, "
            "stdout_tail, stderr_tail}. Use this for long-running conditions like "
            "waiting for a file to appear or a background process to finish — "
            "much more reliable than sleep loops via exec_command (which caps at 30s). "
            "Example: wait_for('test -s /path/to/reward.txt', timeout_s=1500)."
        ),
        strict_mode=False,
    )
    async def wait_for(
        shell: str,
        timeout_s: int = 900,
        poll_interval_s: int = 10,
    ) -> dict[str, Any]:
        import time as _time
        t0 = _time.monotonic()
        last_stdout = ""
        last_stderr = ""
        last_exit: int | None = None
        while True:
            try:
                result = await session.exec(shell, shell=["/bin/bash", "-c"], timeout=30)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                return {
                    "status": "error",
                    "elapsed_s": round(_time.monotonic() - t0, 1),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            last_exit = getattr(result, "exit_code", None)
            raw_stdout = getattr(result, "stdout", b"") or b""
            raw_stderr = getattr(result, "stderr", b"") or b""
            if isinstance(raw_stdout, (bytes, bytearray)):
                raw_stdout = raw_stdout.decode("utf-8", errors="replace")
            if isinstance(raw_stderr, (bytes, bytearray)):
                raw_stderr = raw_stderr.decode("utf-8", errors="replace")
            last_stdout = raw_stdout[-400:]
            last_stderr = raw_stderr[-400:]
            elapsed = _time.monotonic() - t0
            if last_exit == 0:
                return {
                    "status": "ready",
                    "elapsed_s": round(elapsed, 1),
                    "exit_code": last_exit,
                    "stdout_tail": last_stdout,
                    "stderr_tail": last_stderr,
                }
            if elapsed >= timeout_s:
                return {
                    "status": "timeout",
                    "elapsed_s": round(elapsed, 1),
                    "exit_code": last_exit,
                    "stdout_tail": last_stdout,
                    "stderr_tail": last_stderr,
                }
            await asyncio.sleep(min(poll_interval_s, max(1, timeout_s - elapsed)))

    return wait_for


def _make_submit_tool_use_behavior(sink: dict[str, Any]):
    """Stop the agent only after `submit` has accepted and populated `sink`."""

    def tool_use_behavior(_context: Any, tool_results: list[Any]) -> ToolsToFinalOutputResult:
        for tool_result in tool_results:
            tool = getattr(tool_result, "tool", None)
            name = getattr(tool, "name", None)
            qualified_name = getattr(tool, "qualified_name", None)
            if name == "submit" or qualified_name == "submit":
                if sink:
                    return ToolsToFinalOutputResult(
                        is_final_output=True,
                        final_output=getattr(tool_result, "output", None),
                    )
                return ToolsToFinalOutputResult(is_final_output=False, final_output=None)
        return ToolsToFinalOutputResult(is_final_output=False, final_output=None)

    return tool_use_behavior


def _make_submit_tool(sink: dict[str, Any], output_schema: list[dict[str, Any]]):
    """Return a function_tool closed over `sink`. Calling it populates the
    sink and returns; tool_use_behavior ends the loop only after acceptance."""

    @function_tool(
        name_override="submit",
        description_override=(
            "Submit the final structured result for this subtask. Exactly one "
            "call ends the task. Pass success=true with a populated `output` "
            "dict, or success=false with a human-readable `error` string. "
            "If required fields are intentionally impossible to provide, set "
            "dangerously_bypass_required_fields=true."
        ),
        strict_mode=False,
    )
    async def submit(
        success: bool,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        dangerously_bypass_required_fields: bool = False,
    ) -> str:
        if output is not None and not isinstance(output, dict):
            return "submit rejected: `output` must be an object/dict. Please call submit again."
        missing = _missing_required_output_fields(
            output_schema=output_schema,
            success=success,
            output=output or {},
        )
        if missing and not dangerously_bypass_required_fields:
            fields = ", ".join(missing)
            return (
                "submit rejected: missing required output field(s): "
                f"{fields}. Add these fields and call submit again. If the fields are "
                "intentionally impossible to provide, call submit with "
                "dangerously_bypass_required_fields=true and explain why in `error`."
            )
        sink["success"] = bool(success)
        sink["output"] = output or {}
        sink["error"] = error
        if missing:
            sink["required_fields_bypassed"] = missing
        return "submitted"

    return submit


def _missing_required_output_fields(
    *,
    output_schema: list[dict[str, Any]],
    success: bool,
    output: dict[str, Any],
) -> list[str]:
    """Return required output field names omitted from a successful submit."""
    if not success:
        return []
    missing: list[str] = []
    for field_spec in output_schema:
        if not field_spec.get("required", True):
            continue
        name = field_spec["name"]
        if name not in output or output[name] is None:
            missing.append(name)
            continue
        if field_spec["type"] in ("file", "directory") and not str(output[name]).strip():
            missing.append(name)
    return missing


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
            if field_spec.get("required", True):
                emit(f"WARN: required {field_spec['type']} field "
                     f"{field_spec['name']!r} was not set by the agent")
            continue
        sb_path = str(sb_path)
        try:
            if field_spec["type"] == "file":
                local = await _pull_file(session, sb_path, subtask_dir, field_spec["name"])
                detail = _human_size(_safe_size(local))
            else:
                local = await _pull_dir(session, sb_path, subtask_dir, field_spec["name"])
                n, total = _dir_stats(local)
                detail = f"{n} file{'' if n == 1 else 's'}, {_human_size(total)}"
            outputs[field_spec["name"]] = str(local)
            rel = _display_path(local, subtask_dir)
            emit(
                f"saved {field_spec['name']} ({field_spec['type']}, {detail}) "
                f"→ {rel}"
            )
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
        _safe_extract_tar(tf, local)
    return local


def _safe_extract_tar(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract regular files and directories without allowing path escapes."""
    root = dest.resolve()
    for member in tf.getmembers():
        target = (root / member.name).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"unsafe tar member path: {member.name!r}") from exc

        if member.isdir():
            if target.exists() and not target.is_dir():
                raise ValueError(f"tar directory conflicts with file: {member.name!r}")
            target.mkdir(parents=True, exist_ok=True)
            continue

        if not member.isfile():
            raise ValueError(f"unsupported tar member type: {member.name!r}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_dir():
            raise ValueError(f"tar file conflicts with directory: {member.name!r}")
        src = tf.extractfile(member)
        if src is None:
            raise ValueError(f"could not read tar member: {member.name!r}")
        with src, target.open("wb") as out:
            out.write(src.read())


def _close(reader) -> None:
    close = getattr(reader, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_stats(p: Path) -> tuple[int, int]:
    """Return (file_count, total_bytes) for a directory tree. Symlinks and
    unreadable entries are skipped silently."""
    n = 0
    total = 0
    for child in p.rglob("*"):
        if not child.is_file():
            continue
        n += 1
        try:
            total += child.stat().st_size
        except OSError:
            pass
    return n, total


def _human_size(n: int) -> str:
    """Mirror orchestrator._fmt_bytes — KiB/MiB to match upload warnings."""
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{n} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _display_path(local: Path, subtask_dir: Path) -> str:
    """Render `local` relative to the run root (e.g. `subtasks/0000/foo.md`).

    Falls back to the absolute path if `subtask_dir` is not the expected
    `<run>/subtasks/<idx>/` shape (defensive — the orchestrator currently
    always uses that layout)."""
    try:
        run_root = subtask_dir.parent.parent
        return str(local.relative_to(run_root))
    except (ValueError, IndexError):
        return str(local)


# ── Small helpers ───────────────────────────────────────────────────────────


def _collect_secrets(secret_names: list[str]) -> list[modal.Secret]:
    return [modal.Secret.from_name(n) for n in secret_names]


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
