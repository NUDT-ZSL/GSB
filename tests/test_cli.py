from __future__ import annotations

import json
from pathlib import Path

from .conftest import generate_samples, parse_stdout, run_cli, write_jsonl


def _import(sample_dir: Path, workspace: Path, name: str) -> dict:
    result = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / name),
    )
    return parse_stdout(result)


def test_import_shuffled_and_exact_duplicates_are_consistent(
    sample_dir: Path, workspace: Path
) -> None:
    first = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_ordered.jsonl"),
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_report = parse_stdout(first)
    first_cases = {
        case["case_id"]: case["event_ids"]
        for case in first_report["case_summaries"]
    }

    shuffled = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_shuffled.jsonl"),
    )
    assert shuffled.returncode == 0, shuffled.stdout + shuffled.stderr
    duplicate = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_duplicates.jsonl"),
    )
    assert duplicate.returncode == 0, duplicate.stdout + duplicate.stderr
    duplicate_report = parse_stdout(duplicate)
    duplicate_cases = {
        case["case_id"]: case["event_ids"]
        for case in duplicate_report["case_summaries"]
    }
    assert duplicate_cases == first_cases
    assert any(
        anomaly["code"] == "E_DUPLICATE_EVENT"
        for anomaly in duplicate_report["anomalies"]
    )


def test_partial_failure_and_argument_error_exit_codes(
    sample_dir: Path, workspace: Path
) -> None:
    partial = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_anomalies.jsonl"),
    )
    assert partial.returncode == 1
    report = parse_stdout(partial)
    assert report["status"] == "partial"
    codes = {reject["code"] for reject in report["rejects"]}
    assert {"E_JSON", "E_MISSING_FIELD"} <= codes
    anomaly_codes = {item["code"] for item in report["anomalies"]}
    assert {
        "E_ORPHAN",
        "E_CYCLE",
        "E_CROSS_TRACE",
        "E_SEQ_GAP",
        "E_EVENT_CONFLICT",
        "E_DUPLICATE_EVENT",
    } <= anomaly_codes

    bad_args = run_cli("import", "--workspace", str(workspace))
    assert bad_args.returncode == 2
    bad_report = json.loads(bad_args.stderr)
    assert bad_report["errors"][0]["code"] == "E_ARGS"


def test_query_export_and_check_commands(
    sample_dir: Path, workspace: Path, tmp_path: Path
) -> None:
    imported = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_ordered.jsonl"),
    )
    assert imported.returncode == 0
    imported_report = parse_stdout(imported)
    case_id = imported_report["case_summaries"][0]["case_id"]

    query = run_cli(
        "query-case",
        "--workspace",
        str(workspace),
        "--case-id",
        case_id,
    )
    assert query.returncode == 0, query.stdout + query.stderr
    query_report = parse_stdout(query)
    assert query_report["case"]["case_id"] == case_id
    assert query_report["case"]["evidence"]

    missing = run_cli(
        "query-case",
        "--workspace",
        str(workspace),
        "--case-id",
        "case-does-not-exist",
    )
    assert missing.returncode == 1
    assert parse_stdout(missing)["errors"][0]["code"] == "E_CASE_NOT_FOUND"

    output_path = tmp_path / "forensics.json"
    report_path = tmp_path / "command-report.json"
    exported = run_cli(
        "export-report",
        "--workspace",
        str(workspace),
        "--output",
        str(output_path),
        "--report",
        str(report_path),
    )
    assert exported.returncode == 0, exported.stdout + exported.stderr
    exported_report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exported_report["status"] == "ok"
    assert len(exported_report["cases"]) == 3
    assert json.loads(report_path.read_text(encoding="utf-8"))["status"] == "ok"

    checked = run_cli(
        "check-checkpoint",
        "--workspace",
        str(workspace),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    check_report = parse_stdout(checked)
    assert check_report["load"]["cases_source"] == "snapshot"
    assert check_report["workspace_write_probe"] is True


def test_rule_override_changes_query_case_view(
    sample_dir: Path, workspace: Path
) -> None:
    result = run_cli(
        "import",
        "--workspace",
        str(workspace),
        "--input",
        str(sample_dir / "events_ordered.jsonl"),
    )
    assert result.returncode == 0
    query = run_cli(
        "query-case",
        "--workspace",
        str(workspace),
        "--rules",
        str(sample_dir / "rules-short-window.json"),
    )
    assert query.returncode == 0, query.stdout + query.stderr
    report = parse_stdout(query)
    assert report["load"]["requires_rebuild"] is True
    assert report["cases"] == []


def test_incremental_checkpoints_in_different_arrival_orders_match(
    sample_dir: Path, tmp_path: Path
) -> None:
    ordered_ws = tmp_path / "ordered-ws"
    shuffled_ws = tmp_path / "shuffled-ws"
    ordered = run_cli(
        "import",
        "--workspace",
        str(ordered_ws),
        "--input",
        str(sample_dir / "events_ordered.jsonl"),
        "--checkpoint-every",
        "1",
    )
    shuffled = run_cli(
        "import",
        "--workspace",
        str(shuffled_ws),
        "--input",
        str(sample_dir / "events_shuffled.jsonl"),
        "--checkpoint-every",
        "1",
    )
    assert ordered.returncode == 0, ordered.stdout + ordered.stderr
    assert shuffled.returncode == 0, shuffled.stdout + shuffled.stderr
    ordered_report = parse_stdout(ordered)
    shuffled_report = parse_stdout(shuffled)
    ordered_identity = sorted(
        (case["case_id"], tuple(case["event_ids"]))
        for case in ordered_report["case_summaries"]
    )
    shuffled_identity = sorted(
        (case["case_id"], tuple(case["event_ids"]))
        for case in shuffled_report["case_summaries"]
    )
    assert ordered_identity == shuffled_identity
    assert len(ordered_report["normalized_events"]) == 9
    assert len(shuffled_report["normalized_events"]) == 9

