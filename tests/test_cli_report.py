from __future__ import annotations

import json
from pathlib import Path

from conftest import (
    SAMPLES,
    cli_json,
    event,
    run_cli,
    samples_dir,
    write_jsonl,
)


def test_end_to_end_report_truth_and_graph(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    report = tmp_path / "report.json"
    graph = tmp_path / "graph.json"
    assert cli_json(
        "import-events",
        "--workspace",
        workspace,
        "--events",
        samples_dir / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        samples_dir / "rules_snapshot.json",
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
        "--patch",
        samples_dir / "patch-003-address.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", workspace)["exit_code"] == 0
    exported = cli_json(
        "export-report",
        "--workspace",
        workspace,
        "--output",
        report,
        "--graph",
        graph,
        "--truth",
        samples_dir / "truth.json",
    )
    assert exported["exit_code"] == 0
    parsed = json.loads(report.read_text(encoding="utf-8"))
    graph_data = json.loads(graph.read_text(encoding="utf-8"))
    assert parsed["summary"]["event_count"] == 12
    assert parsed["summary"]["inconsistent_count"] == 0
    assert parsed["truth_validation"]["matched_marker_count"] == 12
    assert parsed["truth_validation"]["mismatch_count"] == 0
    assert parsed["events"] == sorted(
        parsed["events"],
        key=lambda item: (item["event_id"], item["seq"]),
    )
    assert any(
        item["rule_name"] == "contact-phone"
        and item["generation"] == 3
        for item in parsed["rules"]["active"]
    )
    assert graph_data["schema"] == "audit-mask-dependency-graph/v1"
    assert graph_data["rules"]
    assert parsed["business_codes"]


def test_batch_import_partial_failure_continues(tmp_path):
    path = tmp_path / "mixed.jsonl"
    lines = [
        json.dumps(event("ok-1")),
        "{not-json",
        json.dumps(event("ok-2")),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = cli_json(
        "import-events",
        "--workspace",
        tmp_path / "ws",
        "--events",
        path,
    )
    assert result["exit_code"] == 1
    assert result["accepted"] == 2
    assert result["rejected"] == 1
    assert result["business_codes"]["E_PARSE"] == 1
    assert result["_process_exit"] == 1


def test_rule_conflict_is_persisted_in_report(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    report = tmp_path / "report.json"
    assert cli_json(
        "import-events",
        "--workspace",
        workspace,
        "--events",
        samples_dir / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        samples_dir / "rules_conflict.json",
    )["exit_code"] == 0
    recomputed = cli_json("recompute", "--workspace", workspace)
    assert recomputed["exit_code"] == 1
    assert recomputed["business_codes"]["E_RULE_CONFLICT"] == 1
    exported = cli_json(
        "export-report",
        "--workspace",
        workspace,
        "--output",
        report,
    )
    assert exported["exit_code"] == 0
    parsed = json.loads(report.read_text(encoding="utf-8"))
    assert parsed["summary"]["rule_conflict_count"] == 1
    assert parsed["business_codes"]["E_RULE_CONFLICT"] >= 1
    conflict = parsed["conflicts"][0]
    assert conflict["rule_name"] == "conflict-email"
    assert len(conflict["field_values"]) == 2


def test_argument_error_exit_code_is_two(tmp_path):
    result = run_cli("recompute")
    assert result.returncode == 2


def test_generated_samples_are_reproducible(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    script = SAMPLES.parent / "generate_samples.py"
    one = run_cli("--version")
    assert one.returncode == 0
    completed_one = __import__("subprocess").run(
        [__import__("sys").executable, str(script), "--out", str(first)],
        cwd=SAMPLES.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    completed_two = __import__("subprocess").run(
        [__import__("sys").executable, str(script), "--out", str(second)],
        cwd=SAMPLES.parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed_one.returncode == 0
    assert completed_two.returncode == 0
    names = sorted(path.name for path in first.iterdir())
    assert names == sorted(path.name for path in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes()
    truth = json.loads((first / "truth.json").read_text(encoding="utf-8"))
    assert truth["event_count"] == 12
    for item in truth["events"]:
        assert "sync_marker" in item
        assert "raw_payload_utf8_hex" in item
        assert "expected" not in item
