from __future__ import annotations

import asyncio
import io
import json
import tarfile
from types import SimpleNamespace

import modal
import pytest
from agents.tool_context import ToolContext

from wide_research import cli, doctor
from wide_research.runner import (
    _collect_secrets,
    _make_submit_tool,
    _make_submit_tool_use_behavior,
    _missing_required_output_fields,
    _safe_extract_tar,
)


def _invoke_submit(submit, args: dict) -> str:
    raw = json.dumps(args)
    ctx = ToolContext(
        context=None,
        tool_name="submit",
        tool_call_id="test-call",
        tool_arguments=raw,
    )
    return asyncio.run(submit.on_invoke_tool(ctx, raw))


def _tar_with(member: tarfile.TarInfo, data: bytes = b"") -> io.BytesIO:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        if member.isfile():
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data))
        else:
            tf.addfile(member)
    buf.seek(0)
    return buf


def test_load_env_precedence(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo.env"
    tool_home = tmp_path / "tool"
    cwd = tmp_path / "work"
    extra = tmp_path / "extra.env"
    tool_home.mkdir()
    cwd.mkdir()

    repo.write_text("OPENAI_API_KEY=repo\n")
    (tool_home / ".env").write_text(
        "OPENAI_API_KEY=tool\nOPENAI_BASE_URL=https://tool.example/api/\n"
    )
    (cwd / ".env").write_text(
        "OPENAI_API_KEY=cwd\nOPENAI_BASE_URL=https://cwd.example/api/\n"
    )
    extra.write_text(
        "OPENAI_API_KEY=extra\nOPENAI_BASE_URL=https://extra.example/api/\n"
    )

    monkeypatch.setenv("OPENAI_API_KEY", "shell")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://shell.example/api/")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "_repo_env_if_source_install", lambda: repo)
    monkeypatch.setattr(cli.paths, "tool_home", lambda: tool_home)

    cli._load_env(extra)

    assert cli.os.environ["OPENAI_API_KEY"] == "extra"
    assert cli.os.environ["OPENAI_BASE_URL"] == "https://extra.example/api/"


def test_doctor_env_discovery_matches_run_precedence(monkeypatch, tmp_path) -> None:
    repo = tmp_path / "repo.env"
    tool_home = tmp_path / "tool"
    cwd = tmp_path / "work"
    extra = tmp_path / "extra.env"
    tool_home.mkdir()
    cwd.mkdir()

    repo.write_text("OPENAI_API_KEY=repo\n")
    (tool_home / ".env").write_text("OPENAI_API_KEY=tool\n")
    (cwd / ".env").write_text("OPENAI_API_KEY=cwd\n")
    extra.write_text("OPENAI_API_KEY=extra\n")

    monkeypatch.setenv("OPENAI_API_KEY", "shell")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(doctor, "_repo_env_if_source_install", lambda: repo)
    monkeypatch.setattr(doctor.paths, "tool_home", lambda: tool_home)

    assert doctor._credential_source("OPENAI_API_KEY", extra) == str(extra)
    assert doctor._credential_source("OPENAI_API_KEY") == str(cwd / ".env")


def test_collect_secrets_does_not_passthrough_host_openai_key(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-secret")
    secrets = _collect_secrets([])

    assert secrets == []


def test_collect_secrets_keeps_explicit_modal_secrets(monkeypatch) -> None:
    seen: list[str] = []

    def fake_from_name(name: str):
        seen.append(name)
        return f"secret:{name}"

    monkeypatch.setattr(modal.Secret, "from_name", fake_from_name)

    assert _collect_secrets(["sandbox-token"]) == ["secret:sandbox-token"]
    assert seen == ["sandbox-token"]


def test_submit_rejects_missing_required_fields_until_bypassed() -> None:
    schema = [
        {"name": "ceo", "type": "string", "required": True},
        {"name": "notes", "type": "string", "required": False},
    ]
    sink: dict = {}
    submit = _make_submit_tool(sink, schema)

    msg = _invoke_submit(submit, {"success": True, "output": {}})
    assert "missing required output field(s): ceo" in msg
    assert sink == {}

    decision = _make_submit_tool_use_behavior(sink)(
        None,
        [SimpleNamespace(tool=submit, output=msg)],
    )
    assert decision.is_final_output is False

    msg = _invoke_submit(
        submit,
        {
            "success": True,
            "output": {},
            "dangerously_bypass_required_fields": True,
        },
    )
    assert msg == "submitted"
    assert sink["success"] is True
    assert sink["required_fields_bypassed"] == ["ceo"]

    decision = _make_submit_tool_use_behavior(sink)(
        None,
        [SimpleNamespace(tool=submit, output=msg)],
    )
    assert decision.is_final_output is True


def test_missing_required_output_fields_allows_falsey_scalars() -> None:
    schema = [
        {"name": "count", "type": "number", "required": True},
        {"name": "flag", "type": "boolean", "required": True},
        {"name": "artifact", "type": "file", "required": True},
    ]

    assert _missing_required_output_fields(
        output_schema=schema,
        success=True,
        output={"count": 0, "flag": False, "artifact": ""},
    ) == ["artifact"]


def test_safe_extract_tar_writes_regular_file(tmp_path) -> None:
    member = tarfile.TarInfo("./nested/result.txt")
    with tarfile.open(fileobj=_tar_with(member, b"ok"), mode="r") as tf:
        _safe_extract_tar(tf, tmp_path)

    assert (tmp_path / "nested" / "result.txt").read_text() == "ok"


def test_safe_extract_tar_rejects_path_escape(tmp_path) -> None:
    member = tarfile.TarInfo("../../escape.txt")
    with tarfile.open(fileobj=_tar_with(member, b"nope"), mode="r") as tf:
        with pytest.raises(ValueError, match="unsafe tar member path"):
            _safe_extract_tar(tf, tmp_path)

    assert not (tmp_path.parent / "escape.txt").exists()


def test_safe_extract_tar_rejects_symlink(tmp_path) -> None:
    member = tarfile.TarInfo("link")
    member.type = tarfile.SYMTYPE
    member.linkname = "/etc/passwd"

    with tarfile.open(fileobj=_tar_with(member), mode="r") as tf:
        with pytest.raises(ValueError, match="unsupported tar member type"):
            _safe_extract_tar(tf, tmp_path)
