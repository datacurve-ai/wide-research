"""Orchestrator: build per-subtask specs, dispatch to Modal, collect results.

Harness runs on the host (one `Runner.run` per subtask). Per-subtask state is
written to `<run>/state/<idx>.json` so `wr tail` and external watchers can
follow progress live."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import modal

from wide_research.config import WideResearchConfig
from wide_research.image import build_image
from wide_research.prompt import render_prompt
from wide_research.runner import RunEnv, Submission, SubtaskSpec, run_subtask


log = logging.getLogger("wide_research.orchestrator")


# ── Pre-flight guardrails for uploads ───────────────────────────────────────
# wide-research is meant for many small isolated sandboxes, not for shipping
# big trees around. We warn well before failure so users notice.

_UPLOAD_FILE_COUNT_WARN = 10
_UPLOAD_TOTAL_BYTES_WARN = 50 * 1024 * 1024          # 50 MiB
_UPLOAD_SINGLE_BYTES_ERROR = 500 * 1024 * 1024       # 500 MiB per path


@dataclass(slots=True)
class SubtaskResult:
    index: int
    input: str
    payload: Submission
    log_path: Path


# ── Public entry point ──────────────────────────────────────────────────────


async def run(
    cfg: WideResearchConfig,
    *,
    run_dir: Path,
    on_modal_ready: Callable[[dict[str, Any]], None] | Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> list[SubtaskResult]:
    """Execute every subtask. Returns results in input order.

    `on_modal_ready` fires as soon as the Modal app is resolved and
    `modal.json` is written — useful for printing the dashboard URL up
    front, before subtasks start spinning up."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(exist_ok=True)

    app = await asyncio.to_thread(
        modal.App.lookup, cfg.modal_app_name, create_if_missing=True,
    )
    image = build_image(cfg.image)

    env_info = _modal_env_info(app)
    log.info(
        "modal app=%s env=%s profile=%s  %s",
        env_info["app_name"], env_info["environment"], env_info["profile"],
        env_info["dashboard_url"] or "",
    )
    (run_dir / "modal.json").write_text(json.dumps(env_info, indent=2))
    if on_modal_ready is not None:
        result = on_modal_ready(env_info)
        if asyncio.iscoroutine(result):
            await result

    if not _host_openai_key_present():
        log.warning(
            "no OPENAI_API_KEY in local env — agent runs will likely fail "
            "to reach OpenAI"
        )

    specs = _build_specs(cfg)
    _preflight_uploads(specs)

    env = RunEnv(
        app=app,
        image=image,
        resources=cfg.resources,
        secret_names=cfg.secrets,
        app_name=cfg.modal_app_name,
        enable_docker=cfg.image.enable_docker,
        sandbox_timeout=cfg.timeout_seconds + 120,  # setup + teardown buffer
    )

    sem = asyncio.Semaphore(cfg.parallelism) if cfg.parallelism else None
    state_writer = _make_state_writer(run_dir / "state")

    async def _one(spec: SubtaskSpec) -> SubtaskResult:
        log_path = run_dir / "logs" / f"{spec.index:04d}.log"
        subtask_dir = run_dir / "subtasks" / f"{spec.index:04d}"
        payload = await run_subtask(
            spec,
            env=env,
            log_path=log_path,
            subtask_dir=subtask_dir,
            semaphore=sem,
            state_writer=state_writer,
        )
        return SubtaskResult(
            index=spec.index,
            input=spec.input,
            payload=payload,
            log_path=log_path,
        )

    tasks = [asyncio.create_task(_one(s)) for s in specs]
    results: list[SubtaskResult] = []
    for coro in asyncio.as_completed(tasks):
        res = await coro
        ok = res.payload.get("success")
        log.info(
            "subtask %d/%d %s %s",
            res.index + 1,
            len(specs),
            "OK" if ok else "FAIL",
            "" if ok else f"({res.payload.get('error')!r})",
        )
        results.append(res)
    results.sort(key=lambda r: r.index)
    return results


# ── Spec building + upload guardrails ───────────────────────────────────────


def _build_specs(cfg: WideResearchConfig) -> list[SubtaskSpec]:
    schema = [f.model_dump() for f in cfg.output_schema]
    specs: list[SubtaskSpec] = []
    for idx, raw_input in enumerate(cfg.inputs):
        rp = render_prompt(cfg.prompt_template, raw_input, workdir=cfg.image.workdir)
        upload: dict[str, str] = dict(cfg.mount_files)
        upload.update(rp.files)
        specs.append(
            SubtaskSpec(
                index=idx,
                name=cfg.name,
                title=cfg.title,
                brief=cfg.brief,
                input=raw_input,
                prompt=rp.text,
                model=cfg.model,
                max_turns=cfg.max_turns,
                timeout_seconds=cfg.timeout_seconds,
                output_schema=schema,
                workdir=cfg.image.workdir,
                upload_files=upload,
            )
        )
    return specs


def _preflight_uploads(specs: list[SubtaskSpec]) -> None:
    """Warn on too many uploads / too-large payloads before launching.

    wide-research is designed for `Pool.map()`-style fan-out; shipping big
    shared trees to every sandbox burns money and wall-clock for no reason.
    Use `mount_files` or `<file>` tags sparingly."""
    seen: dict[str, int] = {}
    for spec in specs:
        for host in spec.upload_files:
            if host in seen:
                continue
            p = Path(host)
            if not p.exists():
                continue
            size = _path_size(p)
            seen[host] = size
            if size > _UPLOAD_SINGLE_BYTES_ERROR:
                raise ValueError(
                    f"upload too large: {host} is {_fmt_bytes(size)} "
                    f"(limit {_fmt_bytes(_UPLOAD_SINGLE_BYTES_ERROR)}). "
                    "Subset the data or reshape the job."
                )

    total = sum(seen.values())
    if len(seen) > _UPLOAD_FILE_COUNT_WARN:
        log.warning(
            "uploading %d distinct host paths per sandbox — prefer ≤%d for snappy runs",
            len(seen), _UPLOAD_FILE_COUNT_WARN,
        )
    if total > _UPLOAD_TOTAL_BYTES_WARN:
        log.warning(
            "total upload size per sandbox ≈ %s (soft limit %s); big folders slow every run",
            _fmt_bytes(total), _fmt_bytes(_UPLOAD_TOTAL_BYTES_WARN),
        )


def _path_size(p: Path) -> int:
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    if not p.is_dir():
        return 0
    total = 0
    for child in p.rglob("*"):
        if child.is_file():
            try:
                total += child.stat().st_size
            except OSError:
                pass
    return total


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n}B"


# ── Env + state plumbing ────────────────────────────────────────────────────


def _host_openai_key_present() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _modal_env_info(app: "modal.App") -> dict[str, str | None]:
    """Best-effort Modal app/environment metadata for the run manifest.

    All fields fail open to None — we only use this for logging + the
    manifest, never for routing."""
    info: dict[str, str | None] = {
        "app_name": getattr(app, "name", None),
        "app_id": getattr(app, "_app_id", None) or getattr(app, "app_id", None),
        "environment": None,
        "profile": None,
        "dashboard_url": None,
    }
    try:
        from modal import config as mc
        info["profile"] = getattr(mc, "_profile", None)
        try:
            info["environment"] = mc.config.get("environment")
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass
    if info["app_id"]:
        info["dashboard_url"] = f"https://modal.com/id/{info['app_id']}"
    return info


def _make_state_writer(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    latest: dict[int, dict[str, Any]] = {}

    def write(index: int, delta: dict[str, Any]) -> None:
        merged = dict(latest.get(index, {}))
        merged.update(delta)
        latest[index] = merged
        path = state_dir / f"{index:04d}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, default=str))
        tmp.replace(path)

    return write
