#!/usr/bin/env python3
"""Reproduce the acceptance scenarios and write a compact evidence bundle."""

from __future__ import annotations

import copy
import hashlib
import json
import platform
import shutil
import sys
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from table_features.canonical import atomic_write_json, pretty_json_bytes  # noqa: E402
from table_features.config import load_config, parse_config  # noqa: E402
from table_features.data import generate_rows, write_jsonl  # noqa: E402
from table_features.errors import BusinessError  # noqa: E402
from table_features.injection import load_injection_config  # noqa: E402
from table_features.pipeline import (  # noqa: E402
    Pipeline,
    write_result_artifacts,
)
from table_features.snapshots import SnapshotStore  # noqa: E402


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summary(result: Any, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "status": "ok" if not result.errors else "degraded",
        "matrix_shape": [int(result.matrix.shape[0]), int(result.matrix.shape[1])],
        "matrix_version": result.matrix_digest,
        "selected_features": result.selected_features,
        "selected_count": len(result.selected_features),
        "scores": result.scores,
        "error_codes": list(dict.fromkeys(result.error_codes)),
        "errors": [error.to_dict() for error in result.errors],
        "unclassified_errors": [error.to_dict() for error in result.unclassified_errors],
        "degradations": [degradation.to_dict() for degradation in result.degradations],
        "cache_hit_count": sum(
            1 for lookup in result.diagnostics.cache_lookups if lookup.status == "hit"
        ),
        "cache_recomputed_count": sum(
            1 for lookup in result.diagnostics.cache_lookups if lookup.status != "hit"
        ),
        "snapshots": [write.to_dict() for write in result.snapshot_writes],
    }
    if extra:
        payload.update(extra)
    return payload


def scenario_config(cfg: Any, scenario: str) -> Any:
    raw = copy.deepcopy(cfg.raw)
    if scenario == "category_encoding":
        raw["category_features"][0] = {
            "name": "color",
            "encoding": "target",
            "smoothing": 5.0,
            "folds": 3,
            "seed": 11,
        }
    elif scenario == "slot_add":
        raw["cross_slots"].append(
            {
                "slot_id": "color_channel",
                "columns": ["color", "channel"],
                "representation": "one_hot",
                "column_prefix": "x",
                "name_separator": "__",
                "value_separator": "|",
            }
        )
    elif scenario == "selector_change":
        raw["selector"] = {
            "type": "variance",
            "mode": "top_k",
            "target_count": 6,
            "threshold": 0.05,
            "bins": 4,
            "default_variance_threshold": 0.0,
        }
    elif scenario == "config_version_bump":
        raw["config_version"] = 2
    else:
        raise ValueError(f"unknown scenario: {scenario}")
    return parse_config(raw)


def scenario_rows(rows: list[dict[str, Any]], scenario: str) -> list[dict[str, Any]]:
    changed = copy.deepcopy(rows)
    if scenario == "numeric_row":
        changed[5]["age"] += 3
    elif scenario == "label_row":
        changed[0]["label"] = 1 - changed[0]["label"]
    return changed


def cross_column_values(result: Any) -> dict[str, np.ndarray]:
    values: dict[str, np.ndarray] = {}
    for block in [*result.category_blocks, *result.numeric_blocks, *result.cross_blocks]:
        for index, column in enumerate(block.columns):
            values[column] = block.matrix[:, index].copy()
    return values


def main() -> int:
    demo_root = ROOT / "artifacts" / "demo"
    if demo_root.exists():
        shutil.rmtree(demo_root)
    demo_root.mkdir(parents=True)

    rows = generate_rows(20240517, 24)
    sample_path = ROOT / "data" / "samples.jsonl"
    sample_hash = write_jsonl(sample_path, rows)
    cfg = load_config(ROOT / "config" / "features.json")
    injection = load_injection_config(ROOT / "config" / "injections.json")

    normal_state = demo_root / "normal-state"
    normal_pipeline = Pipeline(
        normal_state / "cache",
        normal_state / "snapshots",
        clear=True,
    )
    normal_first = normal_pipeline.run(rows, cfg)
    normal_first_hashes = write_result_artifacts(normal_first, demo_root / "normal-first")
    normal_second = normal_pipeline.run(rows, cfg)
    normal_second_hashes = write_result_artifacts(normal_second, demo_root / "normal-second")

    compared_files = [
        "matrix.npy",
        "standardization.json",
        "encoding.json",
        "cross.json",
        "selection.json",
    ]
    reproducibility = {
        filename: (
            (demo_root / "normal-first" / filename).read_bytes()
            == (demo_root / "normal-second" / filename).read_bytes()
        )
        for filename in compared_files
    }
    color_cache = normal_pipeline.cache.inspect_feature("color")
    region_score_cache = normal_pipeline.cache.inspect_feature("cat__region__target")

    injected_state = demo_root / "injected-state"
    injected = Pipeline(
        injected_state / "cache",
        injected_state / "snapshots",
        clear=True,
    ).run(rows, cfg, injection)
    write_result_artifacts(injected, demo_root / "injected")

    clean_values = cross_column_values(normal_first)
    injected_values = cross_column_values(injected)
    preserved_columns = [
        column
        for column in ("cat__region__target", "num__age", "num__tenure")
        if column in clean_values and column in injected_values
    ]
    preserved = {
        column: bool(np.array_equal(clean_values[column], injected_values[column]))
        for column in preserved_columns
    }

    incremental_report: dict[str, Any] = {}
    scenarios = [
        "numeric_row",
        "category_encoding",
        "slot_add",
        "selector_change",
        "label_row",
        "config_version_bump",
    ]
    for scenario in scenarios:
        state = demo_root / "incremental" / scenario / "state"
        output = demo_root / "incremental" / scenario / "output"
        pipeline = Pipeline(state / "cache", state / "snapshots", clear=True)
        old_rows = rows
        new_rows = scenario_rows(rows, scenario)
        new_cfg = cfg if scenario in {"numeric_row", "label_row"} else scenario_config(cfg, scenario)
        pipeline.run(old_rows, cfg)
        result, incremental = pipeline.run_incremental(old_rows, cfg, new_rows, new_cfg)
        write_result_artifacts(result, output)
        atomic_write_json(output / "incremental.json", incremental.to_dict(), pretty=True)
        incremental_report[scenario] = {
            "old_config_version": cfg.config_version,
            "new_config_version": new_cfg.config_version,
            "affected_stages": incremental.plan.affected_stages,
            "affected_features": incremental.plan.affected_features,
            "recompute_features": incremental.observed_recomputed_features,
            "reused_features": incremental.observed_reused_features,
            "recompute_stages": incremental.recompute_stages,
            "recompute_reuse_disjoint": not bool(
                set(incremental.observed_recomputed_features)
                & set(incremental.observed_reused_features)
            ),
            "numeric_standardization_kept_cross_unaffected": (
                scenario != "numeric_row"
                or incremental.plan.affected_features["cross"] == []
            ),
            "cache_hits": summary(result)["cache_hit_count"],
            "cache_recomputed": summary(result)["cache_recomputed_count"],
        }

    snapshot_state = demo_root / "snapshot-state"
    store = SnapshotStore(snapshot_state, clear=True)
    first_snapshot = store.put("statistics", 1, {"payload": "v1"})
    second_snapshot = store.put("statistics", 2, {"payload": "v2"})
    rollback_code: str | None = None
    try:
        store.put("statistics", 1, {"payload": "rollback"})
    except BusinessError as exc:
        rollback_code = exc.code
    store.put("selection", 1, {"selected": ["a"]})
    selection_path = snapshot_state / "selection.json"
    tampered = json.loads(selection_path.read_text(encoding="utf-8"))
    tampered["payload"]["selected"] = ["tampered"]
    selection_path.write_text(json.dumps(tampered), encoding="utf-8")
    corrupt_code: str | None = None
    try:
        store.get("selection")
    except BusinessError as exc:
        corrupt_code = exc.code

    report = {
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "sample": {
            "path": str(sample_path.relative_to(ROOT)),
            "rows": len(rows),
            "sha256": sample_hash,
        },
        "config": {
            "config_version": cfg.config_version,
            "statistics_version": cfg.statistics_version,
            "selection_version": cfg.selection_version,
            "digest": cfg.digest,
            "slots_digest": cfg.slots_digest,
        },
        "normal_run": summary(normal_first),
        "normal_rerun": summary(normal_second),
        "reproducibility": {
            "byte_identical_files": reproducibility,
            "all_byte_identical": all(reproducibility.values()),
            "first_artifact_hashes": normal_first_hashes,
            "second_artifact_hashes": normal_second_hashes,
        },
        "cache_query": {
            "color_entries": color_cache,
            "cat__region__target_entries": region_score_cache,
        },
        "injection": summary(
            injected,
            {
                "successfully_encoded_columns_preserved": preserved,
                "errors_by_stage": [
                    {
                        "stage": error.stage,
                        "code": error.code,
                        "feature": error.feature,
                        "row_id": error.row_id,
                    }
                    for error in injected.errors
                ],
            },
        ),
        "incremental": incremental_report,
        "snapshots": {
            "create": first_snapshot.to_dict(),
            "replace_new_version": second_snapshot.to_dict(),
            "rollback_attempt_code": rollback_code,
            "corruption_probe_code": corrupt_code,
        },
    }
    atomic_write_json(demo_root / "demo_report.json", report, pretty=True)
    (demo_root / "verification.md").write_bytes(
        (
            "# Reproduction evidence\n\n"
            f"- Python: {report['environment']['python']}\n"
            f"- NumPy: {report['environment']['numpy']}\n"
            f"- Sample SHA-256: `{sample_hash}`\n"
            f"- Matrix version: `{normal_first.matrix_digest}`\n"
            f"- Selected features ({len(normal_first.selected_features)}): "
            f"`{', '.join(normal_first.selected_features)}`\n"
            f"- Byte-identical rerun: `{all(reproducibility.values())}`\n"
            f"- Cache hits on rerun: `{summary(normal_second)['cache_hit_count']}`\n"
            f"- Injection error codes: "
            f"`{', '.join(dict.fromkeys(injected.error_codes))}`\n"
            f"- Preserved successful columns: `{preserved}`\n"
            f"- All incremental recompute/reuse sets disjoint: "
            f"`{all(item['recompute_reuse_disjoint'] for item in incremental_report.values())}`\n"
            f"- Snapshot rollback code: `{rollback_code}`\n"
            f"- Snapshot corruption code: `{corrupt_code}`\n"
        ).encode("utf-8")
    )
    sys.stdout.buffer.write(pretty_json_bytes(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

