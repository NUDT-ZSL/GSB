from __future__ import annotations

import json
from pathlib import Path

from memguard.codes import (
    E_ACCESS_KIND,
    E_ACCESS_RANGE_OVERFLOW,
    E_ACCESS_SEQ_DUPLICATE,
    E_ACCESS_SEQ_GAP,
    E_ARENA_SIZE,
    E_INPUT_JSON,
    E_OFFSET,
    E_PARENT_CYCLE,
    E_PARENT_MISSING,
    E_REGION_ID_DUPLICATE,
    E_REGION_UNKNOWN,
    E_TIMESTAMP,
    E_TRACE_ID_DUPLICATE,
    OOB_ANCESTOR_CONTAINED,
    OOB_CROSS_ANCESTOR,
    OOB_ENDPOINT_TOUCH,
    OOB_OUTSIDE_ANCESTORS,
    OOB_ZERO_LENGTH,
)
from memguard.engine import RunOptions, run
from memguard.evaluator import evaluate_access
from memguard.models import Region, TraceRecord
from memguard.parser import parse_region_files, parse_trace_object

from conftest import read_jsonl, write_jsonl


def test_parser_rejects_bad_records_and_continues(sample: Path, tmp_path: Path) -> None:
    result = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl", sample / "zz_invalid_traces.jsonl"],
            database_path=tmp_path / "ledger.sqlite",
            report_path=tmp_path / "report.json",
            truth_marker_paths=[sample / "sync_markers.json"],
            max_workers=2,
            batch_size=3,
        )
    )
    codes = {item["business_code"] for item in result.report["rejections"]}
    assert {
        E_INPUT_JSON,
        E_REGION_UNKNOWN,
        E_ACCESS_KIND,
        E_OFFSET,
        E_ACCESS_RANGE_OVERFLOW,
        E_TIMESTAMP,
        E_ACCESS_SEQ_DUPLICATE,
    } <= codes
    assert result.report["batch"]["access_counts"]["total"] == 15
    assert result.report["truth"]["ok"] is True
    for rejection in result.report["rejections"]:
        assert rejection["source_file"]
        assert "business_code" in rejection
    malformed = [
        item
        for item in result.report["rejections"]
        if item["business_code"] == E_INPUT_JSON
    ]
    assert malformed and malformed[0]["line_number"] == 1
    assert result.exit_code == 1


def test_trace_and_region_uniqueness_and_validation(sample: Path, tmp_path: Path) -> None:
    records = read_jsonl(sample / "traces.jsonl")
    duplicate_trace = dict(records[0])
    duplicate_trace["access_seq"] = 40
    bad_arena = dict(records[0])
    bad_arena["trace_id"] = "bad-arena"
    bad_arena["access_seq"] = 41
    bad_arena["arena_size"] = 0
    bad_range = dict(records[0])
    bad_range["trace_id"] = "bad-range"
    bad_range["access_seq"] = 42
    bad_range["base_offset"] = (1 << 64) - 1
    bad_range["access_size"] = 1
    bad_trace_path = tmp_path / "bad_traces.jsonl"
    write_jsonl(
        bad_trace_path,
        records + [duplicate_trace, bad_arena, bad_range],
    )
    region_document = json.loads((sample / "regions.json").read_text(encoding="utf-8"))
    duplicate_region_path = tmp_path / "duplicate_regions.json"
    duplicate_region_path.write_text(
        json.dumps(
            {
                "regions": region_document["regions"]
                + [region_document["regions"][0]]
            }
        ),
        encoding="utf-8",
    )

    result = run(
        RunOptions(
            region_paths=[sample / "regions.json", duplicate_region_path],
            trace_paths=[bad_trace_path],
            database_path=tmp_path / "ledger.sqlite",
            report_path=tmp_path / "report.json",
            max_workers=1,
        )
    )
    codes = {item["business_code"] for item in result.report["rejections"]}
    assert E_TRACE_ID_DUPLICATE in codes
    assert E_ARENA_SIZE in codes
    assert E_ACCESS_RANGE_OVERFLOW in codes
    assert E_REGION_ID_DUPLICATE in codes


def test_verdict_codes_and_normalized_evidence(sample: Path, run_sample) -> None:
    result, report_path = run_sample(workers=1, batch_size=7)
    assert json.loads(report_path.read_text(encoding="utf-8"))["schema_version"] == 1
    by_seq = {item["access_seq"]: item for item in result.report["accesses"]}
    assert by_seq[1]["business_code"] == "OK"
    assert by_seq[2]["business_code"] == "OK"
    assert by_seq[3]["business_code"] == OOB_ANCESTOR_CONTAINED
    assert by_seq[3]["evidence"]["first_oob_offset"] == 3072
    assert by_seq[3]["evidence"]["allowed_intersection"] == [3000, 3072]
    assert by_seq[3]["evidence"]["ancestor_region_id"] == "region-child"
    assert by_seq[4]["business_code"] == OOB_CROSS_ANCESTOR
    assert by_seq[4]["evidence"]["first_oob_offset"] == 4000
    assert by_seq[5]["business_code"] == OOB_ENDPOINT_TOUCH
    assert by_seq[5]["evidence"]["first_oob_offset"] == 3072
    assert by_seq[6]["business_code"] == OOB_ENDPOINT_TOUCH
    assert by_seq[6]["evidence"]["first_oob_offset"] == 1016
    assert by_seq[7]["business_code"] == OOB_ZERO_LENGTH
    assert by_seq[7]["evidence"]["zero_length_offset"] == 2000
    assert by_seq[8]["business_code"] == E_PARENT_CYCLE
    assert set(by_seq[8]["evidence"]["cycle_region_ids"]) == {
        "region-cyc-a",
        "region-cyc-b",
    }
    assert by_seq[9]["business_code"] == E_PARENT_MISSING
    assert by_seq[9]["evidence"]["missing_parent_region_id"] == "region-missing"
    assert by_seq[12]["business_code"] == OOB_OUTSIDE_ANCESTORS
    assert by_seq[13]["business_code"] == OOB_OUTSIDE_ANCESTORS
    assert by_seq[21]["business_code"] == OOB_OUTSIDE_ANCESTORS

    leaf = next(item for item in result.report["regions"] if item["region_id"] == "region-leaf")
    assert leaf["directions"]["read"]["counts"]["out_of_bounds"] == 3
    assert leaf["directions"]["write"]["counts"]["out_of_bounds"] == 2
    assert leaf["directions"]["read"]["ledger"]["oob_upper_bound"] == 5000
    assert leaf["directions"]["write"]["ledger"]["oob_upper_bound"] == 3080

    sequence = result.report["sequence_diagnostics"]
    assert any(
        item["business_code"] == E_ACCESS_SEQ_GAP
        and item["missing_ranges"] == [{"start": 14, "end": 19}]
        for item in sequence
    )
    assert any(
        item["business_code"] == E_ACCESS_SEQ_DUPLICATE
        and item["access_seq"] == 3
        and item["duplicate_count"] == 1
        for item in sequence
    )
    assert result.report["truth"]["ok"] is True


def test_expected_metadata_does_not_affect_detection(sample: Path, tmp_path: Path) -> None:
    baseline = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[sample / "traces.jsonl"],
            database_path=tmp_path / "base.sqlite",
            report_path=tmp_path / "base.json",
            max_workers=1,
        )
    )
    records = read_jsonl(sample / "traces.jsonl")
    for record in records:
        if record["access_seq"] != 3:
            record["expected_business_code"] = OOB_OUTSIDE_ANCESTORS
            record["expected_interval"] = [0, 0]
    mutated_path = tmp_path / "mutated.jsonl"
    write_jsonl(mutated_path, records)
    mutated = run(
        RunOptions(
            region_paths=[sample / "regions.json"],
            trace_paths=[mutated_path],
            database_path=tmp_path / "mutated.sqlite",
            report_path=tmp_path / "mutated.json",
            max_workers=4,
        )
    )
    def detection_only(report):
        return [
            {
                key: value
                for key, value in item.items()
                if key not in {"source_file", "line_number"}
            }
            for item in report["accesses"]
        ]

    assert detection_only(baseline.report) == detection_only(mutated.report)
    assert baseline.report["ledger"]["rows"] == mutated.report["ledger"]["rows"]


def test_region_parser_schema_rejection(tmp_path: Path) -> None:
    bad = tmp_path / "bad_regions.json"
    bad.write_text('{"regions":[{"region_id":"x"}]}', encoding="utf-8")
    regions, rejections, _ = parse_region_files([bad])
    assert not regions
    assert rejections[0].business_code == "E_REGION_SCHEMA"
    assert rejections[0].source_file == str(bad)


def test_trace_parser_overflow_is_rejected() -> None:
    region = Region(
        "r",
        "a",
        1024,
        0,
        1024,
        0,
        10,
        1,
        None,
        "regions.json",
        1,
    )
    record, rejection = parse_trace_object(
        {
            "trace_id": "t",
            "access_seq": 1,
            "region_id": "r",
            "base_offset": (1 << 64) - 1,
            "access_size": 2,
            "access_kind": "read",
            "arena_size": 1024,
            "timestamp_ms": 1,
        },
        "trace.jsonl",
        1,
        {"r": region},
    )
    assert record is None
    assert rejection is not None
    assert rejection.business_code == E_ACCESS_RANGE_OVERFLOW


def test_region_lifetime_is_enforced() -> None:
    parent = Region(
        "parent",
        "arena",
        4096,
        0,
        4096,
        0,
        50,
        1,
        None,
        "regions.json",
        1,
    )
    child = Region(
        "child",
        "arena",
        4096,
        100,
        500,
        0,
        100,
        2,
        "parent",
        "regions.json",
        2,
    )
    regions = {"parent": parent, "child": child}

    def trace(timestamp: int) -> TraceRecord:
        return TraceRecord(
            trace_id="t-%d" % timestamp,
            access_seq=1,
            region_id="child",
            base_offset=200,
            access_size=10,
            access_kind="read",
            arena_size=4096,
            timestamp_ms=timestamp,
            source_file="traces.jsonl",
            line_number=1,
        )

    assert evaluate_access(trace(75), regions).business_code == "E_LIFETIME_ANCESTOR"
    assert evaluate_access(trace(100), regions).business_code == "E_LIFETIME_SELF"
    assert evaluate_access(trace(25), regions).business_code == "OK"
