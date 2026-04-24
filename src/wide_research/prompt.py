"""Prompt rendering + `<file>...</file>` tag resolution.

`<file>/host/path</file>` anywhere in `prompt_template` or in the interpolated
`input` means: upload the host file into the sandbox once per subtask, and
rewrite the tag in the rendered prompt to point at the sandbox-side path."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import StrictUndefined, Template


_FILE_TAG_RE = re.compile(r"<file>([^<>]+)</file>")


@dataclass
class RenderedPrompt:
    text: str                             # final prompt delivered to the agent
    files: dict[str, str] = field(default_factory=dict)  # host_path → sandbox_path

    def sandbox_paths(self) -> list[str]:
        return sorted(set(self.files.values()))


def _sandbox_path_for(host_path: str, workdir: str) -> str:
    p = Path(host_path)
    if not p.is_absolute():
        raise ValueError(f"<file> path must be absolute: {host_path!r}")
    digest = hashlib.sha1(host_path.encode()).hexdigest()[:8]
    return f"{workdir.rstrip('/')}/input/{digest}_{p.name}"


def render_prompt(
    template: str,
    input_value: str,
    *,
    workdir: str = "/workspace",
    header: str | None = None,
) -> RenderedPrompt:
    """Render a subtask prompt.

    Uses Jinja2 with `{{input}}` available. After interpolation we scan the
    result for `<file>...</file>` tags and rewrite each to the sandbox path;
    the returned `files` mapping tells the runner what to upload.
    """
    t = Template(template, undefined=StrictUndefined, keep_trailing_newline=True)
    rendered = t.render(input=input_value)

    files: dict[str, str] = {}

    def _sub(m: re.Match[str]) -> str:
        host = m.group(1).strip()
        sb = _sandbox_path_for(host, workdir)
        files[host] = sb
        return sb

    rewritten = _FILE_TAG_RE.sub(_sub, rendered)
    if header:
        rewritten = header.rstrip() + "\n\n" + rewritten
    return RenderedPrompt(text=rewritten, files=files)
