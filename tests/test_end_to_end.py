from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from rope_attention.demo import run_demo, run_fault_demo


def test_full_demo_contains_required_audit_sections(tmp_path: Path) -> None:
    result = run_demo(tmp_path)
    assert result["ok"]
    assert "E_SEMANTIC_DRIFT" in result["business_codes"]
    assert result["sample_verification"]["ok"]
    assert set(result["config_version_views"]) == {"v1", "v2", "v3"}
    upgrade = result["incremental"]["stages"][1]
    assert upgrade["label"] == "upgrade-v1-to-v3-same-semantics"
    assert upgrade["incremental"]["recomputed_nodes"] == []
    block = result["incremental"]["stages"][2]
    assert block["byte_equal_to_full_recompute"]["all_byte_identical"]
    assert block["incremental"]["reused_nodes"]
    assert result["cache_snapshot_control"]["cache_verify"]["ok"]
    for run in result["version_runs"].values():
        assert all(item["byte_identical"] for item in run["reproducibility"].values())
        assert all(
            case["block_deviation"]["within_tolerance"]
            for case in run["cases"].values()
        )


def test_fault_demo_covers_typed_failures(tmp_path: Path) -> None:
    result = run_fault_demo(tmp_path)
    assert result["ok"]
    assert "E_VERSION_UNSUPPORTED" in result["business_codes"]
    assert "E_FORMAT_CORRUPT" in result["business_codes"]
    assert "E_SEMANTIC_DRIFT" in result["business_codes"]
    assert "E_CACHE_CORRUPT" in result["business_codes"]
    assert "E_RESOURCE_LIMIT" in result["business_codes"]
    assert "E_TOLERANCE_EXCEEDED" in result["business_codes"]


def test_cli_writes_json_report(tmp_path: Path) -> None:
    output = tmp_path / "reports" / "demo.json"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rope_attention.cli",
            "--root",
            str(tmp_path),
            "demo",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["schema"] == "rope-attention-demo/v1"
    assert document["ok"]

