"""Config schema for wide-research.

Configs are TOML. Required fields describe the map (brief, name, prompt
template, inputs, output schema); the rest cover Modal sandbox setup and
per-run tuning.

Example shape (see examples/ for full files):

    brief = "Short description of the operation"
    name = "snake_case_identifier"
    title = "Human Readable Title"
    model = "openai/gpt-5.6-sol"
    target_count = 3
    inputs = ["one", "two", "three"]

    prompt_template = '''
    Your template here with {{ input }}.
    '''

    [[output_schema]]
    name = "thing"
    type = "string"
    title = "Thing"
    description = "What the thing is."
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SNAKE_CASE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RESERVED_FIELD_NAMES = frozenset({"meta", "_meta", "error", "success"})


class OutputField(BaseModel):
    """One field each subtask may return via `submit(output={...})`.

    Required by default. Set `required = false` to let the agent omit the
    field — missing file/directory paths are silently skipped during
    collection, and missing scalar fields land as empty strings in the
    aggregated CSV.
    """

    name: str
    type: Literal["string", "number", "boolean", "file", "directory"]
    title: str
    description: str
    format: str = "NA"
    required: bool = True

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not SNAKE_CASE_RE.match(v):
            raise ValueError(f"output field name {v!r} must be lowercase snake_case")
        if v in _RESERVED_FIELD_NAMES:
            raise ValueError(f"output field name {v!r} is reserved")
        return v


class Resources(BaseModel):
    """Minimum compute for each sandbox. Modal's defaults are 0.125 CPU /
    128 MiB — these are *requests*, and containers can burst above the
    request when the worker has capacity. Our defaults here are sized for
    light research/scraping work; bump them up for compile-heavy or
    container-in-container loads."""

    cpu: float = 0.25     # CPU cores requested. Modal min: 0.125.
    memory: int = 512     # MiB requested. Modal min: 128.
    gpu: str | None = None  # e.g. "T4", "A10G"; None = no GPU


class SandboxImage(BaseModel):
    """How to build the per-subtask sandbox image."""

    base_image: str = "python:3.12-slim"
    apt_packages: list[str] = Field(default_factory=list)
    pip_packages: list[str] = Field(default_factory=list)
    run_commands: list[str] = Field(default_factory=list)
    dockerfile: str | None = None
    workdir: str = "/workspace"
    enable_docker: bool = False  # one-click Docker-in-sandbox (DinD)


class WideResearchConfig(BaseModel):
    """A complete wide-research job."""

    # ── Required ────────────────────────────────────────────────────────────
    brief: str
    name: str
    title: str
    prompt_template: str
    target_count: int
    inputs: list[str]
    output_schema: list[OutputField]

    # ── Optional, with slim defaults ───────────────────────────────────────
    model: str = "openai/gpt-5.6-sol"
    reasoning_effort: str | None = None  # e.g. "low"/"medium"/"high"/"xhigh" (reasoning models only)
    parallelism: int | None = None       # None = all at once
    max_turns: int = 100
    timeout_seconds: int = 1800
    resources: Resources = Field(default_factory=Resources)
    image: SandboxImage = Field(default_factory=SandboxImage)
    mount_files: dict[str, str] = Field(default_factory=dict)
    secrets: list[str] = Field(default_factory=list)
    modal_app_name: str = "wide-research"
    output_dir: str | None = None

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not SNAKE_CASE_RE.match(v):
            raise ValueError(f"config name {v!r} must be lowercase snake_case")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> "WideResearchConfig":
        if len(self.inputs) != self.target_count:
            raise ValueError(
                f"inputs length ({len(self.inputs)}) != target_count ({self.target_count}). "
                "They must match."
            )
        names = [f.name for f in self.output_schema]
        if len(names) != len(set(names)):
            raise ValueError("output_schema has duplicate field names")
        if not self.output_schema:
            raise ValueError("output_schema must define at least one field")
        return self

    # ── Loaders ─────────────────────────────────────────────────────────────

    @classmethod
    def from_toml(cls, path: Path | str) -> "WideResearchConfig":
        p = Path(path)
        with p.open("rb") as f:
            data = tomllib.load(f)
        return cls.model_validate(data)

    @classmethod
    def from_toml_text(cls, text: str) -> "WideResearchConfig":
        data = tomllib.loads(text)
        return cls.model_validate(data)

    def sample(self, n: int) -> "WideResearchConfig":
        """Return a copy with only the first `n` inputs (for smoke tests)."""
        if n < 0:
            raise ValueError("sample size must be non-negative")
        n = min(n, len(self.inputs))
        return self.model_copy(update={"inputs": self.inputs[:n], "target_count": n})
