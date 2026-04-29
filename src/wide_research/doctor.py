"""`wide-research doctor` — quick health check for the local install.

Reports:
- CLI is importable + version
- Modal is installed and has a configured token
- OpenAI credential is reachable using the same precedence as `wr run`
- Optional OpenAI-compatible base URL if configured
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


def run_checks(env_file: Path | None = None) -> list[Check]:
    return [
        _check_package(),
        _check_modal_token(),
        _check_openai_key(env_file),
        _check_openai_base_url(env_file),
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


def _check_openai_key(env_file: Path | None) -> Check:
    # Does not contact OpenAI — just confirms a key is reachable.
    source = _credential_source("OPENAI_API_KEY", env_file)
    if source is not None:
        return Check("OPENAI_API_KEY", True, source)
    return Check(
        "OPENAI_API_KEY", False,
        f"set it in {paths.tool_home() / '.env'} (chmod 600), "
        "or export in your shell, or pass --env-file PATH to `wr run`",
    )


def _check_openai_base_url(env_file: Path | None) -> Check:
    # Optional. If present, the OpenAI client/Agents SDK will route through it.
    source = _credential_source("OPENAI_BASE_URL", env_file)
    if source is not None:
        return Check("OPENAI_BASE_URL", True, source)
    return Check("OPENAI_BASE_URL", True, "not set; using OpenAI default")


def _credential_source(name: str, env_file: Path | None = None) -> str | None:
    """Return the winning source for an env var using `wr run` precedence."""
    for candidate in (env_file, Path.cwd() / ".env", paths.tool_home() / ".env"):
        if candidate and candidate.exists() and dotenv_values(candidate).get(name):
            return str(candidate)
    if os.environ.get(name):
        return "present in shell env"
    repo_env = _repo_env_if_source_install()
    if repo_env and repo_env.exists() and dotenv_values(repo_env).get(name):
        return str(repo_env)
    return None


def _repo_env_if_source_install() -> Path | None:
    """Return `<repo>/.env` when running from a source checkout, else None."""
    here = Path(__file__).resolve()
    for p in here.parents:
        if (p / "pyproject.toml").exists() and (p / "src" / "wide_research").exists():
            return p / ".env"
    return None


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
