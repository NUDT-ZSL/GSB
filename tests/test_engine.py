from __future__ import annotations

import json
import shutil

import pytest

from tlsdiag import AnalysisConfig, analyze_run
from tlsdiag import codes
from tlsdiag.budget import LockConflict
from tlsdiag.generate import SampleBuilder
from tlsdiag.locks import file_lock

from conftest import assert_connection_expectation, run_profile


def test_default_profile_matches_expected_truth(sample_dir, expected, tmp_path) -> None:
    run = run_profile(sample_dir, expected, "default", tmp_path / "out", jobs=1)
    report = run.report
    profile = expected["profiles"]["default"]
    assert report["summary"]["connections_total"] == len(profile["connections"])
    for connection_id, expectation in profile["connections"].items():
        assert_connection_expectation(report, connection_id, expectation)

    # Two generations are merged, conflicts are surfaced, tombstones and
    # revocations are visible, and invalid entries did not stop valid ones.
    trust_expectations = expected["trust_expectations"]
    entities = {entity["fingerprint"]: entity for entity in report["trust"]["entities"]}
    conflict_fps = {conflict["fingerprint"] for conflict in report["trust"]["conflicts"]}
    for fingerprint in trust_expectations["conflicts_include"]:
        assert fingerprint in conflict_fps
    for fingerprint in trust_expectations["trusted_include"]:
        assert entities[fingerprint]["trusted"]
    for fingerprint in trust_expectations["tombstones_include"]:
        assert entities[fingerprint]["tombstone"]
    for fingerprint in trust_expectations["revoked_include"]:
        assert entities[fingerprint]["revoked"]
    for fingerprint in trust_expectations["tombstone_coverage_nonempty"]:
        assert entities[fingerprint]["tombstone_covers"]
    trust_codes = {d["code"] for d in report["trust"]["diagnostics"]}
    assert set(trust_expectations["v1_invalid_codes_include"]) <= trust_codes
    assert set(trust_expectations["v2_invalid_codes_include"]) <= trust_codes

    propagation = report["propagation"]
    for expectation in expected["propagation_expectations"]:
        matches = [
            entry
            for entry in propagation
            if entry["fingerprint"] == expectation["fingerprint"]
            and entry["role"] == expectation["role"]
            and entry["reason"] == expectation["reason"]
        ]
        assert matches
        assert matches[0]["affected_connections"] == expectation["affected_connections"]


def test_concurrency_modes_produce_identical_digest(sample_dir, expected, tmp_path) -> None:
    one = run_profile(sample_dir, expected, "default", tmp_path / "jobs1", jobs=1)
    four = run_profile(sample_dir, expected, "default", tmp_path / "jobs4", jobs=4)
    eight = run_profile(sample_dir, expected, "default", tmp_path / "jobs8", jobs=8)
    digests = {
        one.report["summary"]["conclusion_digest"],
        four.report["summary"]["conclusion_digest"],
        eight.report["summary"]["conclusion_digest"],
    }
    assert len(digests) == 1
    for connection_id in one.results:
        assert (
            one.results[connection_id].conclusion_digest
            == four.results[connection_id].conclusion_digest
            == eight.results[connection_id].conclusion_digest
        )


def test_conclusions_do_not_depend_on_input_directory(sample_dir, expected, tmp_path) -> None:
    copied = tmp_path / "copied-input"
    shutil.copytree(sample_dir / "input", copied)
    profile = expected["profiles"]["default"]
    original = run_profile(sample_dir, expected, "default", tmp_path / "original", jobs=2)
    moved = analyze_run(
        handshake_path=copied / "handshake.jsonl",
        v1_path=copied / "trust_v1.json",
        v2_shards=copied / "trust_v2",
        v2_db=copied / "trust_v2.sqlite3",
        output_dir=tmp_path / "moved",
        config=AnalysisConfig(
            jobs=2,
            timeout_ms=profile["config"]["timeout_ms"],
            max_frags_per_connection=profile["config"]["max_frags_per_connection"],
            max_expanded_bytes_per_connection=profile["config"][
                "max_expanded_bytes_per_connection"
            ],
            max_total_expanded_bytes=profile["config"]["max_total_expanded_bytes"],
        ),
    )
    assert (
        original.report["summary"]["conclusion_digest"]
        == moved.report["summary"]["conclusion_digest"]
    )
    assert (
        original.report["trust"]["snapshot_version"]
        == moved.report["trust"]["snapshot_version"]
    )


def test_budget_profile_does_not_stop_other_connections(sample_dir, expected, tmp_path) -> None:
    run = run_profile(sample_dir, expected, "budget", tmp_path / "budget", jobs=2)
    report = run.report
    for connection_id, expectation in expected["profiles"]["budget"]["connections"].items():
        assert_connection_expectation(report, connection_id, expectation)
    by_id = {c["connection_id"]: c for c in report["connections"]}
    assert by_id["conn-frag-budget"]["primary_code"] == codes.E_FRAGS
    assert by_id["conn-byte-budget"]["primary_code"] == codes.E_BUDGET
    assert by_id["conn-budget-fast-ok"]["trust_conclusion"] == "trusted"


def test_timeout_uses_real_clock_and_isolates_fast_connection(
    sample_dir, expected, tmp_path
) -> None:
    timeout_run = run_profile(sample_dir, expected, "timeout", tmp_path / "timeout", jobs=1)
    connection = timeout_run.report["connections"][0]
    assert connection["connection_id"] == "conn-heavy-timeout"
    assert connection["status"] == "timeout"
    assert connection["trust_conclusion"] == "indeterminate"
    assert connection["primary_code"] == codes.E_TIMEOUT
    assert connection["elapsed_ms"] >= 1

    # Mix the expensive connection with a trivial valid one.  The expensive
    # connection times out; the trivial one is still fully processed.
    builder = SampleBuilder(20261125)
    heavy_lines = (sample_dir / "scenarios/timeout.jsonl").read_text(encoding="utf-8")
    fast_records = builder.connection_records(
        "conn-fast-timeout-companion",
        [
            (900, builder.certificate_message(["leaf_ok", "int_ok", "root_alpha"])),
            (901, builder.handshake_message("conn-fast-timeout-companion")),
        ],
        chunk_size=4096,
    )
    mixed = tmp_path / "mixed.jsonl"
    mixed.write_text(
        heavy_lines
        + "\n".join(
            json.dumps(record, sort_keys=True, separators=(",", ":"))
            for record in fast_records
        )
        + "\n",
        encoding="utf-8",
    )
    run = analyze_run(
        handshake_path=mixed,
        v1_path=sample_dir / "input/trust_v1.json",
        v2_shards=sample_dir / "input/trust_v2",
        v2_db=sample_dir / "input/trust_v2.sqlite3",
        output_dir=tmp_path / "mixed-out",
        config=AnalysisConfig(
            jobs=2,
            timeout_ms=20,
            max_frags_per_connection=10000,
            max_expanded_bytes_per_connection=16 * 1024 * 1024,
            max_total_expanded_bytes=256 * 1024 * 1024,
        ),
    )
    by_id = {c["connection_id"]: c for c in run.report["connections"]}
    assert by_id["conn-heavy-timeout"]["primary_code"] == codes.E_TIMEOUT
    assert by_id["conn-fast-timeout-companion"]["trust_conclusion"] == "trusted"

    repeat = run_profile(sample_dir, expected, "timeout", tmp_path / "timeout-2", jobs=3)
    assert (
        timeout_run.report["summary"]["conclusion_digest"]
        == repeat.report["summary"]["conclusion_digest"]
    )


def test_invalid_entries_continue_processing(sample_dir, expected, tmp_path) -> None:
    run = run_profile(sample_dir, expected, "default", tmp_path / "invalid", jobs=1)
    report = run.report
    by_id = {c["connection_id"]: c for c in report["connections"]}
    invalid = by_id["conn-invalid-records"]
    assert invalid["trust_conclusion"] == "trusted"
    assert codes.E_DIRECTION in {issue["code"] for issue in invalid["issues"]}
    global_codes = {d["code"] for d in report["input_diagnostics"]}
    assert codes.E_JSON in global_codes
    assert codes.E_CONN_ID in global_codes


def test_lock_conflict_returns_e_lock(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "locked"
    output.mkdir()
    with file_lock(output / ".tlsdiag.lock"):
        with pytest.raises(LockConflict) as excinfo:
            run_profile(sample_dir, expected, "default", output, jobs=1)
    assert excinfo.value.code == codes.E_LOCK


def test_report_and_graph_are_parseable_json(sample_dir, expected, tmp_path) -> None:
    output = tmp_path / "files"
    run_profile(sample_dir, expected, "default", output, jobs=2)
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    graph = json.loads((output / "graph.json").read_text(encoding="utf-8"))
    checkpoint = json.loads((output / "checkpoint.json").read_text(encoding="utf-8"))
    assert report["schema_version"] == 1
    assert graph["nodes"] and graph["edges"]
    assert checkpoint["completed_count"] == report["summary"]["connections_total"]
    assert report["summary"]["conclusion_digest"]


def test_snapshot_change_marks_whole_snapshot_replacement(
    sample_dir, expected, tmp_path
) -> None:
    output = tmp_path / "snapshot-change"
    first = run_profile(sample_dir, expected, "default", output, jobs=1)
    copied = tmp_path / "new-snapshot"
    shutil.copytree(sample_dir / "input", copied)
    v1 = json.loads((copied / "trust_v1.json").read_text(encoding="utf-8"))
    v1["anchors"].append(
        {
            "alias": "Root Gamma",
            "fingerprint": "sha256:" + "f" * 64,
            "trust_seconds": 0,
            "enabled": True,
        }
    )
    (copied / "trust_v1.json").write_text(json.dumps(v1), encoding="utf-8")
    second = analyze_run(
        handshake_path=copied / "handshake.jsonl",
        v1_path=copied / "trust_v1.json",
        v2_shards=copied / "trust_v2",
        v2_db=copied / "trust_v2.sqlite3",
        output_dir=output,
        config=AnalysisConfig(resume=True),
        resume=True,
    )
    assert first.report["trust"]["snapshot_version"] != second.report["trust"]["snapshot_version"]
    change = second.report["trust"]["version_change"]
    assert change["status"] == "replaced"
    assert change["added"]
    assert second.report["checkpoint"]["rebuild"] is True
    assert "trust_view_version_changed" in second.report["checkpoint"]["rebuild_reasons"]
