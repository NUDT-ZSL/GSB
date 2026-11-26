from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from .errors import LedgerError
from .format_v1 import read_v1, to_v1_dict
from .format_v2 import read_v2, write_v2
from .formats_common import load_json_document
from .generator import generate_csr
from .hashing import read_json, write_json_atomic
from .ingest import validate_csr
from .pipeline import Pipeline
from .sample import generate_sample_case
from .writes import receive_write_batch


def _record(
    name: str,
    stage: str,
    expected_code: str,
    action: Callable[[], Any],
) -> dict[str, Any]:
    try:
        action()
    except LedgerError as exc:
        record = exc.to_record()
        record.update(
            {
                "name": name,
                "expected_code": expected_code,
                "observed_code": exc.code,
                "passed": exc.code == expected_code,
            }
        )
        if not record.get("stage"):
            record["stage"] = stage
        return record
    except Exception as exc:  # noqa: BLE001 - injection suite records all failures
        return {
            "ok": False,
            "name": name,
            "stage": stage,
            "expected_code": expected_code,
            "observed_code": "E_INTERNAL",
            "passed": False,
            "message": f"{type(exc).__name__}: {exc}",
        }
    return {
        "ok": False,
        "name": name,
        "stage": stage,
        "expected_code": expected_code,
        "observed_code": None,
        "passed": False,
        "message": "injection did not raise a business error",
    }


def run_injection_suite(output_root: str | Path) -> dict[str, Any]:
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []

    def struct_nonmonotonic() -> None:
        matrix = generate_csr("inject-struct", 4242, injection="nonmonotonic_row_ptr")
        validate_csr(matrix, stage="struct_ingest")

    records.append(
        _record(
            "struct_nonmonotonic_row_ptr",
            "struct_ingest",
            "E_STRUCT",
            struct_nonmonotonic,
        )
    )

    def struct_column_oob() -> None:
        matrix = generate_csr("inject-struct", 4243, injection="column_oob")
        validate_csr(matrix, stage="struct_ingest")

    records.append(
        _record("struct_column_oob", "struct_ingest", "E_STRUCT", struct_column_oob)
    )

    format_dir = root / "format-cases"
    format_dir.mkdir(exist_ok=True)
    matrix = generate_csr("inject-format", 4244)

    unsupported_path = format_dir / "unsupported.json"
    write_json_atomic(unsupported_path, {"version": 999, "case_id": "x"})
    records.append(
        _record(
            "format_unknown_version",
            "format_dispatch",
            "E_VERSION_UNSUPPORTED",
            lambda: load_json_document(unsupported_path, stage="format_dispatch")
            and __import__("truth_ledger.parsing", fromlist=["read_version"]).read_version(
                read_json(unsupported_path), stage="format_dispatch"
            ),
        )
    )

    bad_field_count = to_v1_dict(matrix)
    bad_field_count["flat_table"][0].append("extra-field")
    bad_field_count_path = format_dir / "bad-field-count.json"
    write_json_atomic(bad_field_count_path, bad_field_count)
    records.append(
        _record(
            "format_row_field_count",
            "format_v1",
            "E_FORMAT",
            lambda: read_v1(bad_field_count_path),
        )
    )

    missing_field = to_v1_dict(matrix)
    missing_field.pop("nrows")
    missing_field_path = format_dir / "missing-field.json"
    write_json_atomic(missing_field_path, missing_field)
    records.append(
        _record(
            "format_field_missing",
            "format_v1",
            "E_FIELD_MISSING",
            lambda: read_v1(missing_field_path),
        )
    )

    bad_number = to_v1_dict(matrix)
    bad_number["flat_table"][0][2] = "not-a-number"
    bad_number_path = format_dir / "bad-number.json"
    write_json_atomic(bad_number_path, bad_number)
    records.append(
        _record(
            "format_unparseable_number",
            "format_v1",
            "E_PARSE",
            lambda: read_v1(bad_number_path),
        )
    )

    dup_doc = to_v1_dict(matrix)
    duplicate_done = False
    for row in range(matrix.nrows):
        start = int(matrix.row_ptr[row])
        end = int(matrix.row_ptr[row + 1])
        if end - start >= 2:
            dup_doc["flat_table"][start + 1][1] = dup_doc["flat_table"][start][1]
            duplicate_done = True
            break
    if not duplicate_done:
        raise RuntimeError("deterministic sample unexpectedly has no row with two entries")
    dup_path = format_dir / "duplicate-index.json"
    write_json_atomic(dup_path, dup_doc)
    records.append(
        _record(
            "format_index_dup",
            "format_v1",
            "E_INDEX_DUP",
            lambda: read_v1(dup_path),
        )
    )

    shard_dir = format_dir / "v2-bad-shard"
    write_v2(shard_dir, matrix)
    manifest_path = shard_dir / "v2_manifest.json"
    manifest = read_json(manifest_path)
    manifest["shards"][0]["row_end"] = matrix.nrows + 99
    write_json_atomic(manifest_path, manifest)
    records.append(
        _record(
            "format_shard_bound",
            "format_v2",
            "E_SHARD_BOUND",
            lambda: read_v2(shard_dir),
        )
    )

    bitmap_dir = format_dir / "v2-bad-bitmap"
    write_v2(bitmap_dir, matrix)
    connection = sqlite3.connect(bitmap_dir / "bitmap_index.sqlite")
    try:
        connection.execute(
            "UPDATE bitmap_index SET bitmap = ? WHERE shard_id = 0",
            (sqlite3.Binary(b"\xff"),),
        )
        connection.commit()
    finally:
        connection.close()
    records.append(
        _record(
            "format_bitmap_oob_or_cover",
            "format_v2",
            "E_BITMAP_COVER",
            lambda: read_v2(bitmap_dir),
        )
    )

    write_case = root / "write-hash-case"
    ws = generate_sample_case(write_case, case_id="inject-write")

    def write_hash_mismatch() -> None:
        event = read_json(ws.events_dir / "write-001.json")
        event["writes"][0]["content_hash"] = "sha256:" + "0" * 64
        receive_write_batch(event, ws.load_initial_csr())

    records.append(
        _record(
            "write_hash_mismatch",
            "write_receive",
            "E_WRITE_CONFLICT",
            write_hash_mismatch,
        )
    )

    regression_event = read_json(ws.events_dir / "write-001.json")
    records.append(
        _record(
            "write_version_regression",
            "write_receive",
            "E_WRITE_CONFLICT",
            lambda: receive_write_batch(
                regression_event, ws.load_initial_csr(), last_version=9
            ),
        )
    )

    overlap_event = read_json(ws.events_dir / "write-001.json")
    if len(overlap_event["writes"]) >= 1:
        first = overlap_event["writes"][0]
        overlap_event["writes"].append(dict(first))
    records.append(
        _record(
            "write_interval_overlap",
            "write_receive",
            "E_WRITE_CONFLICT",
            lambda: receive_write_batch(overlap_event, ws.load_initial_csr()),
        )
    )

    pipeline_write_case = root / "pipeline-write-injection"
    generate_sample_case(pipeline_write_case, case_id="inject-pipeline-write")
    records.append(
        _record(
            "pipeline_write_hash_mismatch",
            "write_receive",
            "E_WRITE_CONFLICT",
            lambda: Pipeline(pipeline_write_case).run_through(
                1, injection="write_hash_mismatch"
            ),
        )
    )

    recompute_case = root / "pipeline-recompute-injection"
    generate_sample_case(recompute_case, case_id="inject-recompute")
    Pipeline(recompute_case).run_through(1)
    records.append(
        _record(
            "recompute_column_oob",
            "incremental_recompute",
            "E_STRUCT",
            lambda: Pipeline(recompute_case).run_through(
                2, injection="column_oob"
            ),
        )
    )

    passed = all(record["passed"] for record in records)
    summary = {
        "ok": passed,
        "total": len(records),
        "passed_count": sum(1 for record in records if record["passed"]),
        "failed_count": sum(1 for record in records if not record["passed"]),
        "records": records,
    }
    write_json_atomic(root / "injection_records.json", summary)
    if not passed:
        raise LedgerError(
            "E_INCONSISTENT",
            "one or more exception injection checks failed",
            stage="injection_suite",
            details={"summary_path": str(root / "injection_records.json")},
        )
    return summary
