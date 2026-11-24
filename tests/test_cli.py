from __future__ import annotations

import json
from pathlib import Path

from supply_chain_audit.cli import main


def test_four_cli_workflows_and_atomic_outputs(sample_paths, tmp_path, capsys):
    workspace = tmp_path / "workspace"
    import_report = tmp_path / "reports" / "import.json"
    query_report = tmp_path / "reports" / "query.json"
    closure_report = tmp_path / "reports" / "closure.json"

    exit_code = main(
        [
            "import-snapshots",
            "--sample-dir",
            sample_paths["root"],
            "--workspace",
            str(workspace),
            "--concurrency",
            "2",
            "--apply-seed-updates",
            "--report",
            str(import_report),
        ]
    )
    assert exit_code == 0
    imported = json.loads(import_report.read_text(encoding="utf-8"))
    assert imported["checkpoint"]["status"] == "complete"
    assert all(
        outcome.get("apply", {}).get("equivalent_to_full_recompute", True)
        for outcome in imported["outcomes"]
        if outcome.get("apply")
    )

    assert main(["query-view", "--workspace", str(workspace), "--status", "revoked", "--out", str(query_report)]) == 0
    query = json.loads(query_report.read_text(encoding="utf-8"))
    assert query["count"] == 2

    assert main(["export-closure", "--workspace", str(workspace), "--out", str(closure_report)]) == 0
    closure = json.loads(closure_report.read_text(encoding="utf-8"))
    assert closure["closure_chains"]
    crypto_report = next(a for a in closure["artifacts"] if a["artifact_key"] == "security/crypto@3.0.1")
    assert crypto_report["error_bound_ms"] in {0, 999, None}
    assert crypto_report["closure_chains"]
    assert any("error_bound_ms" in artifact for artifact in closure["artifacts"])

    assert main(["verify-checkpoint", "--workspace", str(workspace)]) == 0
    assert not list(Path(workspace).rglob("*.tmp"))
    assert (workspace / "checkpoints.sqlite3").exists()
    assert list((workspace / "checkpoints").glob("checkpoint-*.json"))


def test_generate_sample_command(tmp_path):
    output = tmp_path / "generated"
    assert main(["generate-sample", str(output)]) == 0
    assert (output / "v1_snapshot.json").exists()
    assert (output / "v2_manifest.json").exists()
    assert (output / "metadata.sqlite3").exists()
