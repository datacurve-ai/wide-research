"""Detached worker entry point.

`wide-research run` spawns this as a subprocess, redirects its stdout +
stderr to `<run_dir>/worker.log`, and returns immediately. The worker
reads `<run_dir>/config.resolved.toml` (written by the CLI) and executes
the full pipeline.

Signals:
- SIGTERM / SIGINT: cancel the current run gracefully. The runner's
  `finally:` blocks stop each live Modal sandbox, then we exit. The CLI
  command `wide-research stop RUN_DIR` does this for you.

Not meant to be invoked directly. Use `wide-research run` instead."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from wide_research import aggregate, orchestrator
from wide_research.config import WideResearchConfig


log = logging.getLogger("wide_research.worker")


async def _main_async(run_dir: Path) -> int:
    """Run the orchestrator + aggregate. Wired to SIGTERM/SIGINT so the
    whole chain cancels cleanly when `wr stop` sends the signal."""
    loop = asyncio.get_running_loop()
    run_task = asyncio.current_task()

    def _on_signal(sig: signal.Signals) -> None:
        if run_task is None or run_task.done():
            return
        log.info("received %s — cancelling run and tearing down sandboxes", sig.name)
        (run_dir / "WORKER_STOPPING").write_text(sig.name)
        run_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal, sig)

    cfg = WideResearchConfig.from_toml(run_dir / "config.resolved.toml")
    try:
        results = await orchestrator.run(cfg, run_dir=run_dir)
    except asyncio.CancelledError:
        (run_dir / "WORKER_STOPPED").write_text("cancelled via signal")
        log.info("run cancelled; Modal sandboxes stopped in each subtask's finally:")
        return 130

    aggregate.write_results(cfg, results, run_dir=run_dir)
    ok = sum(1 for r in results if r.payload.get("success"))
    log.info("done: %d/%d succeeded", ok, len(results))
    return 0 if ok == len(results) else 1


def main(run_dir_arg: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Quiet the API-call chatter that otherwise dominates worker.log.
    for noisy in ("httpx", "httpcore", "openai.agents", "openai._base_client"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    run_dir = Path(run_dir_arg).resolve()
    resolved = run_dir / "config.resolved.toml"
    if not resolved.exists():
        print(f"worker: no config at {resolved}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_main_async(run_dir))
    except Exception as exc:  # noqa: BLE001
        log.exception("worker crashed: %s", exc)
        (run_dir / "WORKER_FAILED").write_text(f"{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python -m wide_research._worker <run_dir>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
