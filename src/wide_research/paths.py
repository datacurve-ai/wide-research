"""Where we write runs.

Precedence for the runs root (highest to lowest):
  1. `--output` CLI flag                      → acts as the base directory
  2. `output_dir` field in the YAML config    → acts as the base directory
  3. `WIDE_RESEARCH_RUNS_DIR` env var         → acts as the base directory
  4. `$WIDE_RESEARCH_HOME/runs/` (if set)     → env var override of tool home
  5. `~/.wide-research/runs/`                 → default

The actual per-run directory is `<base>/<YYYYMMDD_HHMMSS>_<name>_<slug>/` where
`slug` is a short random suffix so concurrent runs with the same name don't
collide. Pass `--output-exact` / `exact=True` to skip the timestamp+slug wrap."""

from __future__ import annotations

import datetime as _dt
import os
import secrets as _secrets
from pathlib import Path


def tool_home() -> Path:
    """`~/.wide-research/` (or `$WIDE_RESEARCH_HOME` if set)."""
    env = os.environ.get("WIDE_RESEARCH_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".wide-research"


def default_runs_root() -> Path:
    env = os.environ.get("WIDE_RESEARCH_RUNS_DIR")
    if env:
        return Path(env).expanduser()
    return tool_home() / "runs"


def _slug(n: int = 6) -> str:
    return _secrets.token_hex(n // 2 or 3)


def new_run_dir(
    name: str,
    *,
    base: Path | str | None = None,
    exact: bool = False,
    now: _dt.datetime | None = None,
) -> Path:
    """Create and return a fresh per-run directory.

    If `exact=True`, `base` is used as-is (must not already exist).
    Otherwise `<base>/<timestamp>_<name>_<slug>/` is created under `base`
    (defaulting to `default_runs_root()`)."""
    if exact:
        if base is None:
            raise ValueError("exact=True requires base")
        d = Path(base).expanduser()
        d.mkdir(parents=True, exist_ok=False)
    else:
        root = Path(base).expanduser() if base else default_runs_root()
        root.mkdir(parents=True, exist_ok=True)
        now = now or _dt.datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S")
        d = root / f"{stamp}_{name}_{_slug()}"
        d.mkdir(parents=True, exist_ok=False)
    (d / "logs").mkdir(exist_ok=True)
    return d


def list_runs(base: Path | str | None = None) -> list[Path]:
    root = Path(base).expanduser() if base else default_runs_root()
    if not root.exists():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
