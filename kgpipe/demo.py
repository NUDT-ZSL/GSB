from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .generate import generate_sample_data
from .pipeline import run_pipeline
from .snapshot import SnapshotStore
from .util import atomic_write_json, canonical_json
from .verifier import verify_execution_report


def _replace_directory(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _corrupt_first_shard(data_dir: Path) -> Path:
    shard = sorted((data_dir / "shards").glob("*.jsonl"))[0]
    rows = [
        json.loads(line)
        for line in shard.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["char_len"] = rows[0]["char_len"] + 1
    shard.write_text(
        "".join(canonical_json(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return shard


def _corrupt_graph_snapshot(snapshot_dir: Path) -> tuple[Path, dict[str, Any]]:
    manifest = json.loads(
        (snapshot_dir / "manifest.json").read_text(encoding="utf-8")
    )
    entry = manifest["entries"]["graph_stats"]
    path = snapshot_dir / entry["file"]
    wrapper = json.loads(path.read_text(encoding="utf-8"))
    wrapper["payload"]["statistics"]["triple_count"] += 1
    path.write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    store = SnapshotStore(snapshot_dir)
    return path, store.verify_all()


def run_demo(output_dir: str | Path, *, seed: int = 20261121) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    generate_sample_data(data_dir, seed=seed)

    baseline_dir = root / "baseline"
    baseline = run_pipeline(
        data_dir=data_dir,
        out_dir=baseline_dir,
        requested_mode="auto",
        overrides={
            "pair_budget": 12000,
            "memory_limit_mb": 128,
            "approx_neighbors": 4,
            "budget_policy": "error",
        },
    )
    if baseline["status"] != "ok":
        raise RuntimeError(f"基线运行失败: {baseline['status']}")

    verification = verify_execution_report(
        json.loads((baseline_dir / "report.json").read_text(encoding="utf-8")),
        data_dir,
    )
    atomic_write_json(root / "baseline-verification.json", verification)
    if not verification["valid"]:
        raise RuntimeError("基线独立验证失败")

    cross_ok_dir = root / "cross-path-identical"
    cross_ok = run_pipeline(
        data_dir=data_dir,
        out_dir=cross_ok_dir,
        requested_mode="both",
        overrides={
            "pair_budget": 12000,
            "memory_limit_mb": 128,
            "approx_neighbors": 64,
            "budget_policy": "error",
        },
    )
    if (
        cross_ok["status"] != "ok"
        or not cross_ok.get("cross_path", {}).get("statistics_comparable")
    ):
        raise RuntimeError("高邻域双路径一致性验证未通过")

    divergence_dir = root / "cross-path-divergence"
    divergence = run_pipeline(
        data_dir=data_dir,
        out_dir=divergence_dir,
        requested_mode="both",
        overrides={
            "pair_budget": 12000,
            "memory_limit_mb": 128,
            "approx_neighbors": 2,
            "budget_policy": "error",
        },
    )
    divergence_codes = [
        error["code"] for error in divergence.get("errors", [])
    ]
    if "E_PATH_DIVERGENCE" not in divergence_codes:
        raise RuntimeError("低邻域双路径没有触发 E_PATH_DIVERGENCE")

    degraded_dir = root / "degraded-budget"
    degraded = run_pipeline(
        data_dir=data_dir,
        out_dir=degraded_dir,
        requested_mode="auto",
        overrides={
            "pair_budget": 300,
            "memory_limit_mb": 128,
            "approx_neighbors": 3,
            "budget_policy": "degrade",
        },
    )
    if (
        degraded["status"] != "degraded"
        or degraded.get("selection", {}).get("chosen_path") != "approx"
        or degraded.get("selection", {}).get("degradation", {}).get("error_code")
        != "E_PAIR_BUDGET"
    ):
        raise RuntimeError("超预算降级演示未触发 E_PAIR_BUDGET")

    anomaly_data = root / "anomaly-data"
    _replace_directory(data_dir, anomaly_data)
    corrupt_shard = _corrupt_first_shard(anomaly_data)
    anomaly_dir = root / "anomaly-ingest"
    anomaly = run_pipeline(
        data_dir=anomaly_data,
        out_dir=anomaly_dir,
        requested_mode="auto",
        overrides={"pair_budget": 12000},
    )
    anomaly_codes = [error["code"] for error in anomaly.get("errors", [])]
    if "E_INGEST" not in anomaly_codes:
        raise RuntimeError("异常分片没有触发 E_INGEST")

    corrupt_snapshot_dir = root / "corrupt-snapshot"
    _replace_directory(baseline_dir / "snapshots", corrupt_snapshot_dir)
    corrupt_path, snapshot_check = _corrupt_graph_snapshot(corrupt_snapshot_dir)
    atomic_write_json(root / "snapshot-corruption-check.json", snapshot_check)
    if snapshot_check["valid"]:
        raise RuntimeError("损坏快照未被 E_SNAPSHOT_CORRUPT 拦截")
    snapshot_codes = [item["code"] for item in snapshot_check["errors"]]
    if "E_SNAPSHOT_CORRUPT" not in snapshot_codes:
        raise RuntimeError("快照损坏检查缺少 E_SNAPSHOT_CORRUPT")

    replacement_dir = root / "snapshot-replacement"
    if replacement_dir.exists():
        shutil.rmtree(replacement_dir)
    replacement_store = SnapshotStore(replacement_dir)
    replacement_v1 = replacement_store.install(
        "graph_stats",
        1,
        {"graph_version": 1, "triple_count": 1},
    )
    replacement_v2 = replacement_store.install(
        "graph_stats",
        2,
        {"graph_version": 2, "triple_count": 2},
    )
    if (
        replacement_v1["mode"] != "create"
        or replacement_v2["mode"] != "whole_replace"
        or not replacement_store.load("graph_stats")["graph_version"] == 2
    ):
        raise RuntimeError("快照整体替换检查失败")

    summary = {
        "demo_version": 1,
        "seed": seed,
        "data_dir": str(data_dir),
        "baseline": {
            "status": baseline["status"],
            "run_id": baseline["run_id"],
            "summary": baseline["summary"],
            "verification_valid": verification["valid"],
            "report": str(baseline_dir / "report.json"),
        },
        "cross_path_identical": {
            "status": cross_ok["status"],
            "statistics_comparable": cross_ok["cross_path"][
                "statistics_comparable"
            ],
            "aggregate_recall_estimate": cross_ok["cross_path"][
                "aggregate_recall_estimate"
            ],
            "report": str(cross_ok_dir / "report.json"),
        },
        "cross_path_divergence": {
            "status": divergence["status"],
            "error_codes": divergence_codes,
            "first_divergence_doc_id": divergence["cross_path"][
                "first_divergence_doc_id"
            ],
            "first_divergence_triple": divergence["cross_path"][
                "first_divergence_triple"
            ],
            "aggregate_recall_estimate": divergence["cross_path"][
                "aggregate_recall_estimate"
            ],
            "report": str(divergence_dir / "report.json"),
        },
        "budget_degradation": {
            "status": degraded["status"],
            "chosen_path": degraded["selection"]["chosen_path"],
            "degradation": degraded["selection"]["degradation"],
            "warning_codes": [
                warning["code"] for warning in degraded["warnings"]
            ],
            "report": str(degraded_dir / "report.json"),
        },
        "anomaly_ingest": {
            "status": anomaly["status"],
            "error_codes": anomaly_codes,
            "corrupt_shard": str(corrupt_shard),
            "report": str(anomaly_dir / "report.json"),
        },
        "snapshot_corruption": {
            "valid": snapshot_check["valid"],
            "error_codes": snapshot_codes,
            "corrupt_file": str(corrupt_path),
            "report": str(root / "snapshot-corruption-check.json"),
        },
        "snapshot_replacement": {
            "v1": replacement_v1,
            "v2": replacement_v2,
            "current_graph_version": replacement_store.load("graph_stats")[
                "graph_version"
            ],
        },
    }
    atomic_write_json(root / "demo-summary.json", summary)
    return summary

