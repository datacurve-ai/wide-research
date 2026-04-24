"""`wide-research doctor` — quick health check for the local install.

Reports:
- CLI is importable + version
- Modal is installed and has a configured token
- OpenAI credential is reachable (any of: env var, tool-home .env, CWD .env)
- ~/.wide-research layout is sane
- A brief summary of where runs will be written

Doesn't make network calls. Pure local probes."""

from __future__ import annotations

import importlib.metadata as _md
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from wide_research import paths


@dataclass(slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_checks() -> list[Check]:
    return [
        _check_package(),
        _check_modal_token(),
        _check_openai_key(),
        _check_tool_home(),
        _check_runs_root(),
    ]


# ── Individual checks ───────────────────────────────────────────────────────


def _check_package() -> Check:
    try:
        version = _md.version("wide-research")
    except _md.PackageNotFoundError:
        return Check("wide-research installed", False, "package metadata not found")
    return Check("wide-research installed", True, f"version {version}")


def _check_modal_token() -> Check:
    cfg = Path.home() / ".modal.toml"
    if cfg.exists():
        return Check("modal token", True, f"{cfg}")
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return Check("modal token", True, "MODAL_TOKEN_ID/SECRET in env")
    return Check(
        "modal token", False,
        "run `modal token set --token-id ... --token-secret ...` (or set MODAL_TOKEN_ID/SECRET)",
    )


def _check_openai_key() -> Check:
    # Does not contact OpenAI — just confirms a key is reachable.
    if os.environ.get("OPENAI_API_KEY"):
        return Check("OPENAI_API_KEY", True, "present in shell env")
    for candidate in (
        Path.cwd() / ".env",
        paths.tool_home() / ".env",
    ):
        if candidate.exists():
            if dotenv_values(candidate).get("OPENAI_API_KEY"):
                return Check("OPENAI_API_KEY", True, f"{candidate}")
    return Check(
        "OPENAI_API_KEY", False,
        f"set it in {paths.tool_home() / '.env'} (chmod 600), "
        "or export in your shell, or pass --env-file PATH to `wr run`",
    )


def _check_tool_home() -> Check:
    home = paths.tool_home()
    if not home.exists():
        return Check("tool home", True, f"{home} (will be created on first run)")
    bits: list[str] = [f"{home}"]
    env = home / ".env"
    if env.exists():
        try:
            mode = oct(env.stat().st_mode & 0o777)
        except OSError:
            mode = "?"
        bits.append(f".env ({mode})")
    return Check("tool home", True, " · ".join(bits))


def _check_runs_root() -> Check:
    runs = paths.default_runs_root()
    count = 0
    if runs.exists():
        count = sum(1 for p in runs.iterdir() if p.is_dir())
    return Check("runs root", True, f"{runs} · {count} run(s)")
