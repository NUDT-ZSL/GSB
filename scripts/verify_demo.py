#!/usr/bin/env python3
"""Cross-platform acceptance demo for combat balance incremental recomputation."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from balance_lab.canonical import write_json
from balance_lab.evolution import run_evolution
from balance_lab.pipeline import run_pipeline
from balance_lab.sample import SEED, generate_sample_files


def main() -> int:
    sample_dir = ROOT / "samples"
    output_root = ROOT / "artifacts" / "acceptance_demo"
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True)

    generated = generate_sample_files(sample_dir, seed=SEED)
    base = run_pipeline(
        sample_dir,
        output_root / "state",
        output_root / "reports" / "balance_report.json",
        verify=True,
    )
    if base["status"] != "completed":
        print(json.dumps({"ok": False, "stage": "base", "result": base}, default=str))
        return 1

    evolution = run_evolution(
        sample_dir,
        output_root / "edits",
        output_root / "evolution-state",
        output_root / "evolution-reports",
    )
    summary = {
        "ok": True,
        "seed": SEED,
        "generated_files": [str(path.relative_to(ROOT)) for path in generated],
        "base": {
            "balanced": base["report"]["balanced"],
            "consistent": base["verification"]["consistent"],
            "battles": [
                {
                    "battle_id": item["battle_id"],
                    "balanced": item["balanced"],
                    "outcome": item["outcome"],
                    "victory_round": item["victory_round"],
                    "team_hp_ratio": item["team_hp_ratio"],
                    "reward_score": item["reward_score"],
                }
                for item in base["aggregates"]
            ],
        },
        "evolution": {
            "all_consistent": evolution["all_consistent"],
            "total_steps": evolution["total_steps"],
            "total_invalid_chunks": evolution["total_invalid_chunks"],
            "steps": [
                {
                    "step_id": item["step_id"],
                    "changed_keys": item["changed_keys"],
                    "invalid_chunk_count": item["invalid_chunk_count"],
                    "affected_battles": item["affected_battles"],
                    "consistent": item["consistency"]["consistent"],
                }
                for item in evolution["steps"]
            ],
        },
    }
    summary_path = ROOT / "artifacts" / "acceptance_summary.json"
    write_json(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"\nSummary written to {summary_path}")
    return 0 if evolution["all_consistent"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
