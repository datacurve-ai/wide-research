"""Build the Modal image each subtask runs inside.

Images are keyed by content hash, so reusing the same SandboxImage spec across
runs hits the cache. Only `apt_packages` / `pip_packages` / `run_commands` /
`dockerfile` changes invalidate the cache."""

from __future__ import annotations

import os
from pathlib import Path

import modal

from wide_research.config import SandboxImage


# Modal's DinD recipe. Pinned image-builder version is required for the
# sandbox-side `experimental_options={"enable_docker": True}` path.
_DIND_BUILDER_VERSION = "2025.06"
_DIND_APT = (
    "wget", "ca-certificates", "curl", "net-tools", "iproute2", "iptables",
    "docker.io",
)

# start_dockerd.sh ships with this package (runtime/ is just a data directory now).
_START_DOCKERD = Path(__file__).resolve().parent / "runtime" / "start_dockerd.sh"


def build_image(spec: SandboxImage) -> modal.Image:
    """Produce a `modal.Image` from a `SandboxImage` spec.

    Three base options, in priority order:
    1. `spec.dockerfile`            — user Dockerfile path
    2. `spec.base_image` (non-default)   — pull from a registry
    3. `modal.Image.debian_slim(python=3.12)`  — our default
    """
    if spec.enable_docker:
        # The DinD experimental option is only recognised by this builder.
        os.environ.setdefault("MODAL_IMAGE_BUILDER_VERSION", _DIND_BUILDER_VERSION)

    img = _base_image(spec)
    img = _apt_install(img, spec)
    img = _pip_install(img, spec)
    img = _run_commands(img, spec)
    img = img.env({"WR_WORKDIR": spec.workdir})
    return img


# ── internals ───────────────────────────────────────────────────────────────


def _base_image(spec: SandboxImage) -> modal.Image:
    if spec.dockerfile:
        return modal.Image.from_dockerfile(spec.dockerfile)
    if spec.base_image and spec.base_image != "python:3.12-slim":
        return modal.Image.from_registry(spec.base_image)
    return modal.Image.debian_slim(python_version="3.12")


def _apt_install(img: modal.Image, spec: SandboxImage) -> modal.Image:
    apt = list(spec.apt_packages)
    if spec.enable_docker:
        for pkg in _DIND_APT:
            if pkg not in apt:
                apt.append(pkg)
    return img.apt_install(*apt) if apt else img


def _pip_install(img: modal.Image, spec: SandboxImage) -> modal.Image:
    return img.pip_install(*spec.pip_packages) if spec.pip_packages else img


def _run_commands(img: modal.Image, spec: SandboxImage) -> modal.Image:
    commands: list[str] = list(spec.run_commands)
    if spec.enable_docker:
        # DinD entrypoint + iptables-legacy switch. Layered first so
        # user-supplied commands run after docker is in place.
        img = img.add_local_file(str(_START_DOCKERD), "/start-dockerd.sh", copy=True)
        commands = [
            "chmod +x /start-dockerd.sh",
            "update-alternatives --set iptables  /usr/sbin/iptables-legacy",
            "update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy",
            # Debian's docker.io omits the Compose v2 plugin. Install the
            # official static binary so `docker compose ...` works.
            "mkdir -p /usr/local/lib/docker/cli-plugins",
            "curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose "
            "https://github.com/docker/compose/releases/download/v2.31.0/docker-compose-linux-x86_64",
            "chmod +x /usr/local/lib/docker/cli-plugins/docker-compose",
        ] + commands
    return img.run_commands(*commands) if commands else img
