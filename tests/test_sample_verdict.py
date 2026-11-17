from __future__ import annotations

import json
from pathlib import Path

from config_forensics.codes import Code
from config_forensics.engine import EngineConfig, ForensicEngine, canonical_verdict


def _run(root: Path, out_name: str, workers: int, **kwargs):
    engine = ForensicEngine(
        EngineConfig(workers=workers, queue_limit=64, max_file_seconds=5.0)
    )
    return engine.run(
        root / "evidence",
        root / "baseline" / "baseline.json",
        root / out_name / "report.json",
        root / out_name / "graph.json",
        **kwargs,
    )


def test_sample_truth_and_required_findings(sample_root: Path) -> None:
    manifest = json.loads((sample_root / "manifest.json").read_text(encoding="utf-8"))
    truth_records = manifest["truth"]["records"]
    assert truth_records
    for truth in truth_records:
        assert "sender_observed_value" in truth
        assert truth["sync_marker"].startswith("sync:")

    result = _run(sample_root, "out", workers=4)
    report = result.report
    drift = {
        (item["host_id"], item["path"], item["sequence"])
        for item in report["drift_points"]
    }
    assert drift == {
        ("host-alpha", "service.port", 3),
        ("host-beta", "service.port", 2),
        ("host-gamma", "features[0].enabled", 3),
        ("host-hmac", "tls.enabled", 1),
        ("host-endpoint", "quota.limit", 1),
    }
    assert report["tampered"][0]["host_id"] == "host-tamper"
    assert report["tampered"][0]["sequence"] == 1
    assert report["conflicts"][0]["host_id"] == "host-conflict"
    assert any(
        issue["code"] == Code.DIGEST_WIDTH_MISMATCH
        and issue["host_id"] == "host-width"
        for issue in report["errors"]
    )
    assert any(
        issue["code"] == Code.PATH_INVALID and issue["record_index"] == 1
        for issue in report["errors"]
    )

    service_cluster = next(
        cluster
        for cluster in report["event_clusters"]
        if cluster["path"] == "service.port"
    )
    assert service_cluster["host_ids"] == ["host-alpha", "host-beta"]
    assert [member["sequence"] for member in service_cluster["members"]] == [2, 3]

    broken = next(
        chain for chain in report["chains"] if chain["host_id"] == "host-broken"
    )
    assert broken["status"] == "broken"
    assert any(error["code"] == Code.LINK_TARGET_MISSING for error in broken["errors"])
    cycle = next(chain for chain in report["chains"] if chain["host_id"] == "host-cycle")
    assert cycle["status"] == "cycle"
    assert any(error["code"] == Code.LINK_CYCLE for error in cycle["errors"])

    assert result.graph["nodes"]
    assert result.graph["edges"]
    assert all(set(node) >= {"node_id", "tampered", "drift"} for node in result.graph["nodes"])
    saved = json.loads((sample_root / "out" / "report.json").read_text(encoding="utf-8"))
    assert saved == report


def test_serial_and_concurrent_verdicts_are_identical(sample_root: Path) -> None:
    serial = _run(sample_root, "serial", workers=1)
    concurrent = _run(sample_root, "concurrent", workers=8)
    assert canonical_verdict(serial.report) == canonical_verdict(concurrent.report)
    assert serial.graph == concurrent.graph
    assert [item["sequence"] for item in serial.report["evidence"]] == [
        item["sequence"] for item in concurrent.report["evidence"]
    ]


def test_file_and_event_order_are_normalized(sample_root: Path) -> None:
    result = _run(sample_root, "ordered", workers=6)
    file_keys = [
        (item["source_id"], item["file_index"]) for item in result.report["files"]
    ]
    assert file_keys == sorted(file_keys)
    evidence_keys = [
        (
            item["host_id"],
            item["path"],
            item["sequence"],
            item["evidence_id"],
        )
        for item in result.report["evidence"]
    ]
    assert evidence_keys == sorted(evidence_keys)
    cluster_paths = [item["path"] for item in result.report["event_clusters"]]
    assert cluster_paths == sorted(cluster_paths)
