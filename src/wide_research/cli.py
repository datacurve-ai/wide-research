"""wide-research CLI.

`run` is async by default — it spawns a detached worker, prints the run
dir + PID + follow-up hints, and returns. Use `--foreground` / `-f` to
block the current terminal instead.

Two complementary log streams per run:
- `wr tail <run>` — live stream of what each subtask is doing (recommended).
- `<run>/worker.log` — low-level worker log (HTTP/API calls, aggregate).

Commands:
    wide-research doctor                           # check install + credentials
    wide-research run    CONFIG.toml [flags...]    # detached; prints run dir + log path
    wide-research run    CONFIG.toml --foreground  # block until done
    wide-research run    - < CONFIG.toml           # read config from stdin
    wide-research run    --inline 'TOML'           # inline string
    wide-research plan   CONFIG.toml [--index N]   # render one prompt, no sandbox
    wide-research list   [--base DIR]              # past runs under a root
    wide-research inspect RUN_DIR [--failed-only]  # compact table of results
    wide-research tail   RUN_DIR [--poll 0.3]      # merge-tail subtask logs
    wide-research wait   RUN_DIR [--poll 1.0]      # block until finished
    wide-research stop   RUN_DIR [--timeout 30]    # graceful stop; SIGKILL fallback
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Annotated

import tomli_w
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from wide_research import doctor as doctor_mod
from wide_research import orchestrator, paths
from wide_research.aggregate import write_results
from wide_research.config import WideResearchConfig
from wide_research.prompt import render_prompt


app = typer.Typer(
    name="wide-research",
    help="Spawn parallel subtasks in Modal sandboxes and aggregate structured results.",
    no_args_is_help=True,
)
console = Console()


# ── env / config loading ─────────────────────────────────────────────────────


def _repo_env_if_source_install() -> Path | None:
    """Return `<repo>/.env` when running from a source checkout, else None."""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists() and (p / "src" / "wide_research").exists():
            return p / ".env"
    return None


def _load_env(extra: Path | None = None) -> None:
    """Load .env in priority order (first hit wins per var)."""
    for p in (extra, Path.cwd() / ".env", paths.tool_home() / ".env", _repo_env_if_source_install()):
        if p and p.exists():
            load_dotenv(p, override=False)


def _load_config(
    *,
    config_arg: str | None,
    inline: str | None,
) -> tuple[WideResearchConfig, str, Path | None]:
    """Load a TOML config from a path, stdin (`-`), or --inline string."""
    if inline and config_arg:
        console.print("[red]error:[/red] pass either CONFIG or --inline, not both")
        raise typer.Exit(2)
    if not inline and not config_arg:
        console.print("[red]error:[/red] CONFIG path required (use '-' for stdin or --inline)")
        raise typer.Exit(2)

    if inline:
        source_text, source_path = inline, None
    elif config_arg == "-":
        source_text, source_path = sys.stdin.read(), None
    else:
        p = Path(config_arg).expanduser()
        if not p.exists():
            console.print(f"[red]config not found:[/red] {p}")
            raise typer.Exit(2)
        source_text, source_path = p.read_text(), p

    try:
        cfg = WideResearchConfig.from_toml_text(source_text)
    except tomllib.TOMLDecodeError as exc:
        where = str(source_path) if source_path else "--inline / stdin"
        console.print(f"[red]TOML parse error in {where}:[/red] {exc}")
        raise typer.Exit(2) from exc
    except Exception as exc:  # pydantic validation
        where = str(source_path) if source_path else "--inline / stdin"
        console.print(f"[red]config error in {where}:[/red] {exc}")
        raise typer.Exit(2) from exc
    return cfg, source_text, source_path


def _resolve_run_dir(
    cfg: WideResearchConfig,
    *,
    cli_output: Path | None,
    cli_output_exact: Path | None,
) -> Path:
    if cli_output_exact is not None:
        return paths.new_run_dir(cfg.name, base=cli_output_exact, exact=True)
    base = cli_output if cli_output is not None else (Path(cfg.output_dir) if cfg.output_dir else None)
    return paths.new_run_dir(cfg.name, base=base, exact=False)


# ── commands ─────────────────────────────────────────────────────────────────


@app.command("doctor")
def cmd_doctor() -> None:
    """Check local install + credentials. Non-zero exit if anything is missing."""
    results = doctor_mod.run_checks()
    table = Table("check", "status", "detail", show_header=True)
    any_bad = False
    for chk in results:
        mark = "[green]✓[/green]" if chk.ok else "[red]✗[/red]"
        table.add_row(chk.name, mark, chk.detail)
        any_bad = any_bad or not chk.ok
    console.print(table)
    if any_bad:
        console.print(
            "\n[yellow]Some checks failed. "
            "See 'wide-research --help' or docs/ for setup instructions.[/yellow]"
        )
        raise typer.Exit(1)


@app.command("run")
def cmd_run(
    config: Annotated[
        str | None,
        typer.Argument(help="Path to TOML config, or '-' to read from stdin."),
    ] = None,
    inline: Annotated[
        str | None,
        typer.Option("--inline", help="Pass TOML config as a literal string instead of a file."),
    ] = None,
    sample: int | None = typer.Option(None, "--sample", "-s", help="Only run the first N inputs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate + render only; don't spawn sandboxes."),
    output: Path | None = typer.Option(None, "--output", "-o", help="Base dir; wraps run as <base>/<ts>_<name>_<slug>."),
    output_exact: Path | None = typer.Option(None, "--output-exact", help="Exact run dir (must not exist)."),
    env_file: Path | None = typer.Option(None, "--env-file", help="Load OPENAI_API_KEY etc. from this file."),
    foreground: bool = typer.Option(
        False, "--foreground", "-f",
        help="Run synchronously in this terminal instead of detaching.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run a wide-research job (TOML from file, stdin, or --inline).

    Default: spawn the run as a detached background process, print pointers
    (run dir + log file + tail/wait hints), exit immediately. Use
    `--foreground` / `-f` to block this terminal until the run finishes.
    """
    _load_env(env_file)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg, source_text, source_path = _load_config(config_arg=config, inline=inline)
    if sample is not None:
        cfg = cfg.sample(sample)

    if dry_run:
        _print_plan(cfg, source_path)
        return

    run_dir = _resolve_run_dir(cfg, cli_output=output, cli_output_exact=output_exact)
    (run_dir / "config.resolved.toml").write_bytes(
        tomli_w.dumps(cfg.model_dump(exclude_none=True)).encode()
    )
    (run_dir / "config.source.toml").write_text(source_text)

    if foreground:
        _run_foreground(cfg, run_dir)
    else:
        _run_detached(run_dir)


def _run_foreground(cfg: WideResearchConfig, run_dir: Path) -> None:
    console.print(f"[bold]run dir:[/bold] {run_dir}")
    try:
        results = asyncio.run(orchestrator.run(cfg, run_dir=run_dir))
    except KeyboardInterrupt:
        console.print("[yellow]interrupted — partial results may be on Modal[/yellow]")
        raise typer.Exit(130)
    artifacts = write_results(cfg, results, run_dir=run_dir)
    _print_summary(cfg, results, run_dir, artifacts)


def _run_detached(run_dir: Path) -> None:
    """Spawn `python -m wide_research._worker RUN_DIR` as a session leader,
    redirect its stdout/stderr to `<run_dir>/worker.log`, and detach. Print
    abs paths + PID + `wr tail` / `wr wait` hints for the caller."""
    worker_log = run_dir / "worker.log"
    pid_path = run_dir / "worker.pid"
    log_handle = open(worker_log, "w", buffering=1)
    # start_new_session makes the worker its own session leader — surviving
    # the parent's terminal closing. stdin detached from the terminal.
    proc = subprocess.Popen(
        [sys.executable, "-m", "wide_research._worker", str(run_dir)],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    log_handle.close()  # parent's copy — the child inherited its own handle
    pid_path.write_text(f"{proc.pid}\n")

    console.print(f"[bold]run dir:[/bold] {run_dir}")
    console.print(f"[bold]pid:[/bold]     {proc.pid}")
    console.print()
    console.print(f"  wr tail {run_dir}    [dim]# watch what each subtask is doing[/dim]")
    console.print(f"  wr wait {run_dir}    [dim]# block for the final pass/fail summary[/dim]")
    console.print(f"  wr stop {run_dir}    [dim]# stop gracefully (tears down every Modal sandbox)[/dim]")
    console.print(f"  tail -f {worker_log} [dim]# low-level worker log (API calls etc.)[/dim]")


@app.command("plan")
def cmd_plan(
    config: Annotated[str | None, typer.Argument(help="Path to TOML, or '-' for stdin.")] = None,
    inline: Annotated[str | None, typer.Option("--inline")] = None,
    index: int = typer.Option(0, "--index", "-i", help="Which input to render."),
) -> None:
    """Render the prompt for one input without running anything."""
    cfg, _, source_path = _load_config(config_arg=config, inline=inline)
    _print_plan(cfg, source_path, focus_index=index)


@app.command("list")
def cmd_list(
    base: Path | None = typer.Option(None, "--base", "-b", help="Runs root (defaults to ~/.wide-research/runs/)."),
) -> None:
    """List past runs."""
    runs = paths.list_runs(base)
    if not runs:
        console.print("[dim]no runs found[/dim]")
        return
    t = Table("run", "jsonl?", "rows")
    for r in runs:
        jsonl = r / "results.jsonl"
        rows = sum(1 for _ in jsonl.open()) if jsonl.exists() else 0
        t.add_row(str(r), "✓" if jsonl.exists() else "-", str(rows))
    console.print(t)


@app.command("inspect")
def cmd_inspect(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    failed_only: bool = typer.Option(False, "--failed-only"),
) -> None:
    """Print a compact table of results for one run."""
    jsonl = run_dir / "results.jsonl"
    if not jsonl.exists():
        console.print(f"[red]no results.jsonl in {run_dir}[/red]")
        raise typer.Exit(1)
    rows = [json.loads(line) for line in jsonl.open()]
    t = Table("idx", "ok", "input", "error", "output_keys")
    for r in rows:
        if failed_only and r.get("success"):
            continue
        t.add_row(
            str(r.get("index")),
            "✓" if r.get("success") else "✗",
            (r.get("input") or "")[:60],
            (r.get("error") or "")[:60],
            ",".join((r.get("output") or {}).keys()),
        )
    console.print(t)


@app.command("tail")
def cmd_tail(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    poll: float = typer.Option(0.3, "--poll", help="Seconds between polls."),
) -> None:
    """Merge-tail per-subtask logs until the run completes."""
    done = run_dir / "results.jsonl"
    positions: dict[Path, int] = {}

    def _drain() -> None:
        logs = run_dir / "logs"
        if not logs.exists():
            return
        for f in sorted(logs.iterdir()):
            if not f.is_file() or not f.name.endswith(".log"):
                continue
            positions.setdefault(f, 0)
        for f, pos in list(positions.items()):
            try:
                size = f.stat().st_size
            except FileNotFoundError:
                continue
            if size <= pos:
                continue
            with f.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read(size - pos).decode(errors="replace")
            positions[f] = size
            tag = f.stem
            for line in chunk.splitlines():
                console.print(f"[dim][{tag}][/dim] {line}")

    failed = run_dir / "WORKER_FAILED"
    stopped = run_dir / "WORKER_STOPPED"
    while True:
        _drain()
        if done.exists():
            _drain()
            console.print(f"[green]run complete:[/green] {done}")
            return
        if failed.exists():
            _drain()
            console.print(
                f"[red]worker crashed:[/red] {failed.read_text().strip()}\n"
                f"[dim]orchestrator log:[/dim] {run_dir/'worker.log'}"
            )
            return
        if stopped.exists():
            _drain()
            console.print(f"[yellow]run stopped:[/yellow] {stopped.read_text().strip()}")
            return
        time.sleep(poll)


@app.command("wait")
def cmd_wait(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    poll: float = typer.Option(1.0, "--poll", help="Seconds between polls."),
    timeout: float | None = typer.Option(None, "--timeout", help="Max seconds to wait."),
) -> None:
    """Block until the run finishes, then print a one-line summary."""
    done = run_dir / "results.jsonl"
    failed = run_dir / "WORKER_FAILED"
    stopped = run_dir / "WORKER_STOPPED"
    t0 = time.time()
    while not done.exists():
        if failed.exists():
            console.print(
                f"[red]worker crashed[/red] — see {run_dir/'worker.log'}\n"
                f"  {failed.read_text().strip()}"
            )
            raise typer.Exit(1)
        if stopped.exists():
            console.print(f"[yellow]stopped[/yellow] — {stopped.read_text().strip()}")
            raise typer.Exit(130)
        if timeout is not None and (time.time() - t0) > timeout:
            console.print(f"[red]timeout[/red] after {timeout:.0f}s — no results.jsonl at {done}")
            raise typer.Exit(124)
        time.sleep(poll)
    rows = [json.loads(line) for line in done.open()]
    ok = sum(1 for r in rows if r.get("success"))
    total = len(rows)
    color = "green" if ok == total else ("yellow" if ok > 0 else "red")
    console.print(f"[{color}]{ok}/{total}[/{color}] succeeded — {run_dir}")
    raise typer.Exit(0 if ok == total else 1)


@app.command("stop")
def cmd_stop(
    run_dir: Path = typer.Argument(..., exists=True, file_okay=False, dir_okay=True),
    timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait for graceful exit before SIGKILL."),
    force: bool = typer.Option(False, "--force", help="Skip graceful stop — SIGKILL immediately (leaks Modal sandboxes)."),
) -> None:
    """Gracefully stop a running detached job.

    Sends SIGTERM to the worker, which cancels the in-flight orchestrator.
    Each subtask's `finally:` tears down its live Modal sandbox before the
    worker exits. SIGKILL escalation if the worker doesn't exit within
    --timeout."""
    pid_file = run_dir / "worker.pid"
    if not pid_file.exists():
        console.print(f"[red]no worker.pid in {run_dir}[/red]")
        raise typer.Exit(2)
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError as exc:
        console.print(f"[red]malformed worker.pid: {exc}[/red]")
        raise typer.Exit(2) from exc

    if not _pid_alive(pid):
        console.print(f"[dim]worker pid {pid} is not running — nothing to stop[/dim]")
        raise typer.Exit(0)

    if force:
        _send(pid, signal.SIGKILL, "SIGKILL")
        console.print(
            "[yellow]SIGKILL sent — any Modal sandboxes in flight will only "
            "die when their own timeout expires.[/yellow]"
        )
        return

    _send(pid, signal.SIGTERM, "SIGTERM")
    console.print(f"sent SIGTERM to pid {pid}; waiting up to {timeout:.0f}s for graceful exit…")

    t0 = time.time()
    while _pid_alive(pid):
        if time.time() - t0 > timeout:
            console.print(
                f"[yellow]pid {pid} didn't exit in {timeout:.0f}s — escalating to SIGKILL[/yellow]"
            )
            _send(pid, signal.SIGKILL, "SIGKILL")
            return
        time.sleep(0.5)
    console.print("[green]worker stopped cleanly[/green]")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours — still counts as alive.
        return True
    return True


def _send(pid: int, sig: signal.Signals, label: str) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        console.print(f"[dim]pid {pid} already gone[/dim]")


# ── helpers ──────────────────────────────────────────────────────────────────


def _print_plan(cfg: WideResearchConfig, path: Path | None, *, focus_index: int = 0) -> None:
    console.rule(f"[bold]{cfg.title}[/bold]")
    console.print(f"[dim]source:[/dim] {path or '(inline / stdin)'}")
    console.print(f"[dim]brief:[/dim] {cfg.brief}")
    console.print(
        f"[dim]subtasks:[/dim] {len(cfg.inputs)} × model={cfg.model} "
        f"parallelism={cfg.parallelism or 'unbounded'} max_turns={cfg.max_turns} "
        f"timeout={cfg.timeout_seconds}s"
    )
    console.print(
        f"[dim]resources:[/dim] cpu={cfg.resources.cpu} memory={cfg.resources.memory}MiB "
        f"gpu={cfg.resources.gpu or '-'}"
        f"{' docker=on' if cfg.image.enable_docker else ''}"
    )
    schema_rows = Table("name", "type", "title", "format")
    for f in cfg.output_schema:
        schema_rows.add_row(f.name, f.type, f.title, f.format)
    console.print(schema_rows)
    idx = max(0, min(focus_index, len(cfg.inputs) - 1))
    rp = render_prompt(cfg.prompt_template, cfg.inputs[idx], workdir=cfg.image.workdir)
    console.rule(f"rendered prompt for input[{idx}]")
    console.print(rp.text)
    if rp.files:
        console.print()
        console.print("[dim]will upload:[/dim]")
        for host, sb in rp.files.items():
            console.print(f"  {host} → {sb}")


def _print_summary(cfg, results, run_dir, artifacts) -> None:
    ok = sum(1 for r in results if r.payload.get("success"))
    console.rule(f"done: {ok}/{len(results)} succeeded")
    for name, p in artifacts.items():
        console.print(f"  {name}: {p}")
    fails = [r for r in results if not r.payload.get("success")]
    if fails:
        console.print(f"[yellow]{len(fails)} failed — see logs in {run_dir/'logs'}[/yellow]")


if __name__ == "__main__":
    app()
