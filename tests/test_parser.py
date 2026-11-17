from __future__ import annotations

import json
from collections import Counter

from klp_audit.config import AuditConfig
from klp_audit.constants import FILE_HEADER, FILE_MAGIC, FILE_VERSION
from klp_audit.engine import AuditEngine
from klp_audit.parser import build_file, build_payload, parse_bytes

from .helpers import audit_path, load_json, session_by_id


def _artifact(generated_root, manifest, name: str):
    entry = manifest["files"][name]
    return generated_root / entry["sample"], generated_root / entry["truth"]


def test_legal_sample_truth_uses_sync_markers_and_normalized_params(
    generated_root, manifest
):
    sample_path, truth_path = _artifact(generated_root, manifest, "legal")
    report = audit_path(sample_path, config=AuditConfig(concurrency=1))
    truth = load_json(truth_path)
    assert report["file"]["conclusion"] == "PASS"
    assert report["file"]["sha256"] == truth["sha256"]

    for session_id, truth_events in truth["sessions"].items():
        session = session_by_id(report, session_id)
        report_by_marker = {
            event["sync_marker"]: event for event in session["events"]
        }
        accepted = [event for event in truth_events if event["accepted"]]
        assert len(report_by_marker) == len(accepted)
        for expected in accepted:
            actual = report_by_marker[expected["sync_marker"]]
            assert actual["stage"] == expected["stage"]
            assert actual["algorithm"] == expected["algorithm"]
            assert actual["stage_seq"] == expected["stage_seq"]
            assert actual["params"] == expected["params"]


def test_malformed_records_are_rejected_individually_and_batch_continues(
    generated_root, manifest
):
    sample_path, _truth_path = _artifact(generated_root, manifest, "parse_errors")
    report = audit_path(sample_path, config=AuditConfig(concurrency=2))
    rejected = report["rejected_records"]
    assert Counter(record["code"] for record in rejected) == Counter(
        {
            "E_CRC": 1,
            "E_FORMAT": 1,
            "E_ALGORITHM": 1,
            "E_STAGE": 1,
            "E_PARAM": 2,
        }
    )
    indices = [record["record_index"] for record in rejected]
    assert indices == sorted(indices)
    assert len(indices) == len(set(indices)) == 6
    assert all(record["offset"] is not None for record in rejected)
    survivor = session_by_id(report, "survivor")
    assert survivor["status"] == "pass"
    assert report["file"]["conclusion"] == "FAIL"
    assert report["summary"]["rejected_records"] == 6


def test_header_iteration_ceiling_and_invalid_file_magic(tmp_path):
    payload = build_payload(
        record_index=0,
        session_id="limit",
        stage_id=1,
        algorithm_id=2,
        seq=1,
        iterations=11,
        salt=b"salt",
        output_length=32,
        level=1,
        sync_marker=1,
    )
    encoded = build_file([payload], max_iterations=10)
    parsed = parse_bytes(encoded)
    assert parsed.rejected[0].code == "E_PARAM"
    assert "header.max_iterations" in parsed.rejected[0].message

    bad_magic = bytearray(encoded)
    bad_magic[0:4] = b"NOPE"
    parsed_bad = parse_bytes(bytes(bad_magic))
    assert parsed_bad.fatal_code == "E_FORMAT"
    report = AuditEngine().audit_bytes(bytes(bad_magic), source_name="bad.klp")
    assert report["file"]["conclusion"] == "FAIL"
    assert report["file"]["error_codes"] == ["E_FORMAT"]


def test_strict_header_declares_exact_directory_size(tmp_path):
    payload = build_payload(
        record_index=0,
        session_id="ok",
        stage_id=1,
        algorithm_id=2,
        seq=1,
        iterations=1,
        salt=b"",
        output_length=32,
        level=1,
        sync_marker=1,
    )
    encoded = bytearray(build_file([payload], max_iterations=10))
    header = FILE_HEADER.pack(FILE_MAGIC, FILE_VERSION, 0, 41, 10, 1)
    encoded[0:20] = header
    parsed = parse_bytes(bytes(encoded))
    assert parsed.fatal_code == "E_FORMAT"
    assert "header size mismatch" in parsed.fatal_message


def test_idempotent_duplicate_and_conflicting_sequence(generated_root, manifest):
    idempotent_path, _ = _artifact(generated_root, manifest, "idempotent")
    report = audit_path(idempotent_path)
    session = session_by_id(report, "idempotent")
    assert session["status"] == "pass"
    assert session["deduplicated_records"] == 1

    state_path, _ = _artifact(generated_root, manifest, "state_errors")
    state_report = audit_path(state_path)
    conflict = session_by_id(state_report, "seq-conflict")
    assert "E_SEQ_CONFLICT" in conflict["error_codes"]
    issue = next(
        issue for issue in conflict["issues"] if issue["code"] == "E_SEQ_CONFLICT"
    )
    assert issue["stage_seq"] == 2
    assert issue["details"]["kept_record_index"] != issue["details"][
        "conflicting_record_index"
    ]

