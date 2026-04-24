"""Non-OpenAI smoke test: build the image, spawn a Modal sandbox, exec a
command, round-trip a file, tar+download a directory, terminate.

This exercises the Modal-side plumbing wide-research depends on, without
needing an OpenAI key. The harness now lives on the host (see `runner.py`)
so there's nothing wide_research-shaped to invoke inside the sandbox any
more — this test just makes sure the image + sandbox + filesystem APIs
still work together."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import modal

from wide_research.config import WideResearchConfig
from wide_research.image import build_image


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    cfg = WideResearchConfig.from_toml(repo_root / "examples" / "echo_test.toml")

    print("[smoke] building image...")
    t0 = time.time()
    img = build_image(cfg.image)

    app = modal.App.lookup(cfg.modal_app_name, create_if_missing=True)
    sb = modal.Sandbox.create(
        app=app,
        image=img,
        cpu=1,
        memory=1024,
        timeout=300,
        workdir="/workspace",
    )
    print(f"[smoke] sandbox {sb.object_id} up ({time.time()-t0:.1f}s incl. image)")
    try:
        # Basic exec
        p = sb.exec("bash", "-lc", "echo hello from sandbox")
        for line in p.stdout:
            print("  stdout>", line, end="")
        assert p.wait() == 0, "basic exec failed"

        # Write + read a file via the filesystem API
        sb.filesystem.write_bytes(b"hi from host\n", "/workspace/hello.txt")
        p = sb.exec(
            "bash", "-lc",
            "cat /workspace/hello.txt && "
            "mkdir -p /workspace/output/dir && "
            "echo inner > /workspace/output/dir/inner.txt",
        )
        for line in p.stdout:
            print("  stdout>", line, end="")
        assert p.wait() == 0, "filesystem round-trip failed"

        # Tar a directory, pull bytes back
        p = sb.exec(
            "bash", "-lc",
            "mkdir -p /workspace/.wr_tars && "
            "tar -cf /workspace/.wr_tars/dir.tar -C /workspace/output dir",
        )
        assert p.wait() == 0, "tar failed"
        blob = sb.filesystem.read_bytes("/workspace/.wr_tars/dir.tar")
        print(f"[smoke] got tar: {len(blob)} bytes")

        print("[smoke] OK")
        return 0
    finally:
        sb.terminate()


if __name__ == "__main__":
    sys.exit(main())
