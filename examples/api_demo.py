"""Run the four public entrypoints against the local sample dataset."""

from __future__ import annotations

import json
from pathlib import Path

from transformer_fusion import cache_diagnose, equivalence_report, execute, inject_diagnose
from transformer_fusion.generate_data import generate_dataset


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    if not (data_dir / "manifest.jsonl").exists():
        generate_dataset(data_dir)
    manifest = data_dir / "manifest.jsonl"
    cache_dir = root / "artifacts" / "example_cache"
    snapshot_dir = root / "artifacts" / "example_snapshots"

    execution = execute(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    injections = inject_diagnose(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    invalidation = cache_diagnose(
        manifest,
        changes={
            "case_gelu_perm": {"axis_order": [1, 2, 0]},
            "case_silu_eps_axis": {"eps": 1e-3},
        },
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    equivalence = equivalence_report(
        manifest,
        data_dir=data_dir,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
    )
    print(
        json.dumps(
            {
                "execute": {
                    "ok": execution.ok,
                    **execution.to_dict()["summary"],
                },
                "inject_diagnose": {
                    "ok": injections["ok"],
                    "codes": injections["business_codes"],
                },
                "cache_diagnose": {
                    "ok": invalidation["ok"],
                    "affected_cases": invalidation["affected_cases"],
                    "no_intersection": invalidation["no_intersection"],
                    "union_covers_all": invalidation["union_covers_all_affected_nodes"],
                },
                "equivalence_report": {
                    "ok": equivalence["ok"],
                    "selfcheck_ok": equivalence["selfcheck"]["ok"],
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if execution.ok and injections["ok"] and invalidation["ok"] and equivalence["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
