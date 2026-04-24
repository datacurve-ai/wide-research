"""End-to-end tests for the examples/ TOML configs.

Each example is a parameterised test that runs `wide-research run --sample 1`
against the config and asserts the subtask succeeded. These are real
integration tests — they require Modal + OPENAI_API_KEY credentials and each
takes 10-30s.

Run all (skips slow):
    pytest tests/test_examples.py

Include the slow/flaky ones (e.g. CEO lookup that depends on live web data):
    WR_E2E_SLOW=1 pytest tests/test_examples.py

Run just one:
    pytest tests/test_examples.py -k echo_test
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"

# Examples whose correctness depends on live external state (web, pricing).
# Skipped by default to keep the test suite deterministic and cheap.
_SLOW = {"find_fortune_ceos.toml"}


def _examples() -> list[Path]:
    include_slow = os.environ.get("WR_E2E_SLOW") == "1"
    return sorted(
        p for p in EXAMPLES_DIR.glob("*.toml")
        if include_slow or p.name not in _SLOW
    )


def _run(config: Path, sample: int = 1) -> tuple[Path, int, str, str]:
    """Invoke `wide-research run CONFIG --sample N --foreground --output-exact <tmp>`.

    `--foreground` blocks until the job is done so we can assert on results
    synchronously. `--output-exact` lets us pick the run dir ourselves so we
    don't have to parse it back out of CLI output."""
    run_dir = Path(tempfile.mkdtemp(prefix="wr-e2e-"))
    # --output-exact requires the dir to NOT already exist, so remove ours.
    run_dir.rmdir()
    proc = subprocess.run(
        [
            "wide-research", "run", str(config),
            "--sample", str(sample),
            "--foreground",
            "--output-exact", str(run_dir),
        ],
        capture_output=True,
        text=True,
        timeout=900,
    )
    return run_dir, proc.returncode, proc.stdout, proc.stderr


def _parse_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.mark.parametrize(
    "example",
    _examples(),
    ids=lambda p: p.stem,
)
def test_example_end_to_end(example: Path) -> None:
    run_dir, rc, stdout, stderr = _run(example, sample=1)
    assert rc == 0, (
        f"wide-research exited {rc} for {example.name}\n"
        f"STDOUT:\n{stdout[-2000:]}\nSTDERR:\n{stderr[-2000:]}"
    )

    results_path = run_dir / "results.jsonl"
    assert results_path.exists(), f"no results.jsonl at {results_path}"

    rows = _parse_jsonl(results_path)
    assert rows, "empty results.jsonl"
    for row in rows:
        assert row["success"] is True, (
            f"subtask {row.get('index')} failed: {row.get('error')!r}"
        )
        # File / directory fields should point at a path that exists locally.
        for key, value in (row.get("output") or {}).items():
            if not isinstance(value, str):
                continue
            if value.startswith(str(run_dir)):
                assert Path(value).exists(), (
                    f"output[{key}] path {value} does not exist locally"
                )
