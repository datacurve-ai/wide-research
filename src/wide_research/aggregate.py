"""Aggregate per-subtask Submissions into `results.jsonl` and `results.csv`.

File / directory outputs already live under `<run>/subtasks/NNNN/` by the time
we get here (see `runner._collect_outputs`). We just point at them from the
results files; no zipping or copying. Also emits `summary.json` with cost
and token totals for the whole run."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from wide_research.config import WideResearchConfig
from wide_research.cost import CostBreakdown, sum_costs
from wide_research.orchestrator import SubtaskResult


def write_results(
    cfg: WideResearchConfig,
    results: list[SubtaskResult],
    *,
    run_dir: Path,
) -> dict[str, Path]:
    """Write `results.jsonl` + `results.csv` in run_dir.

    Returns a mapping of artifact name → path for the CLI summary."""
    jsonl_path = run_dir / "results.jsonl"
    csv_path = run_dir / "results.csv"
    schema_names = [f.name for f in cfg.output_schema]

    with jsonl_path.open("w") as jf:
        for r in results:
            record = {
                "index": r.index,
                "input": r.input,
                "success": r.payload.get("success", False),
                "output": r.payload.get("output") or {},
                "error": r.payload.get("error"),
                "meta": r.payload.get("meta") or {},
                "log": str(r.log_path.relative_to(run_dir)) if r.log_path else None,
            }
            jf.write(json.dumps(record, default=str) + "\n")

    summary_path = run_dir / "summary.json"
    summary_path.write_text(json.dumps(_build_summary(cfg, results), indent=2))

    header = ["index", "input", "success", "error", *schema_names]
    with csv_path.open("w", newline="") as cf:
        w = csv.writer(cf)
        w.writerow(header)
        for r in results:
            row: list[object] = [
                r.index,
                r.input,
                bool(r.payload.get("success")),
                r.payload.get("error") or "",
            ]
            out = r.payload.get("output") or {}
            for name in schema_names:
                v = out.get(name, "")
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, default=str)
                row.append(v)
            w.writerow(row)

    return {"jsonl": jsonl_path, "csv": csv_path, "summary": summary_path}


def _build_summary(
    cfg: WideResearchConfig,
    results: list[SubtaskResult],
) -> dict[str, object]:
    breakdowns: list[CostBreakdown] = []
    for r in results:
        cost = (r.payload.get("meta") or {}).get("cost")
        if not cost:
            continue
        try:
            breakdowns.append(CostBreakdown(**cost))
        except TypeError:
            continue
    totals = sum_costs(breakdowns)

    ok = sum(1 for r in results if r.payload.get("success"))
    wall = [
        (r.payload.get("meta") or {}).get("wall_seconds", 0.0) or 0.0
        for r in results
    ]
    return {
        "name": cfg.name,
        "title": cfg.title,
        "model": cfg.model,
        "target_count": cfg.target_count,
        "succeeded": ok,
        "failed": len(results) - ok,
        "wall_seconds_max": max(wall, default=0.0),
        "wall_seconds_sum": sum(wall),
        "cost": totals,
    }
