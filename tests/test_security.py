from __future__ import annotations

import io
import tarfile

import modal
import pytest

from wide_research import cli
from wide_research.runner import _collect_secrets, _safe_extract_tar


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
    (tool_home / ".env").write_text("OPENAI_API_KEY=tool\n")
    (cwd / ".env").write_text("OPENAI_API_KEY=cwd\n")
    extra.write_text("OPENAI_API_KEY=extra\n")

    monkeypatch.setenv("OPENAI_API_KEY", "shell")
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(cli, "_repo_env_if_source_install", lambda: repo)
    monkeypatch.setattr(cli.paths, "tool_home", lambda: tool_home)

    cli._load_env(extra)

    assert cli.os.environ["OPENAI_API_KEY"] == "extra"


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
