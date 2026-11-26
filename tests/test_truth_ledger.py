from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from truth_ledger.adjudicate import adjudicate_from_disk
from truth_ledger.errors import LedgerError
from truth_ledger.format_v1 import read_v1, to_v1_dict, write_v1
from truth_ledger.format_v2 import read_v2, write_v2
from truth_ledger.formats_dispatch import read_ledger
from truth_ledger.generator import generate_csr
from truth_ledger.hashing import read_json, write_json_atomic
from truth_ledger.ingest import validate_csr
from truth_ledger.injections import run_injection_suite
from truth_ledger.migrate import convert_ledger
from truth_ledger.pipeline import Pipeline
from truth_ledger.sample import FIXED_SEED, generate_sample_case
from truth_ledger.selfcheck import run_self_check
from truth_ledger.semantic import assert_semantic_equal
from truth_ledger.writes import receive_write_batch


REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def case_root(tmp_path: Path) -> Path:
    return generate_sample_case(tmp_path / "case-001").root


def _cli_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_fixed_seed_generation_is_byte_deterministic(tmp_path: Path) -> None:
    first = generate_sample_case(
        tmp_path / "a", case_id="same", seed=20261127
    )
    second = generate_sample_case(
        tmp_path / "b", case_id="same", seed=20261127
    )
    first_bytes = first.initial_csr_path.read_bytes()
    second_bytes = second.initial_csr_path.read_bytes()
    assert first_bytes == second_bytes
    assert read_json(first.initial_csr_path)["content_hash"] == read_json(
        second.initial_csr_path
    )["content_hash"]


@pytest.mark.parametrize(
    "injection",
    ["nonmonotonic_row_ptr", "column_oob", "nnz_length"],
)
def test_structure_violations_return_e_struct(injection: str) -> None:
    matrix = generate_csr("bad", 123, injection=injection)
    with pytest.raises(LedgerError) as exc_info:
        validate_csr(matrix, stage="struct_ingest")
    assert exc_info.value.code == "E_STRUCT"
    assert exc_info.value.stage == "struct_ingest"


def test_three_initial_formats_restore_same_csr(case_root: Path) -> None:
    ledger_dir = case_root / "initial_ledgers"
    v1, _ = read_v1(ledger_dir / "v1_ledger.json")
    v2, _ = read_v2(ledger_dir / "v2")
    v3, _ = read_ledger(3, ledger_dir / "v3_ledger.json")
    assert v1.digest == v2.digest == v3.digest


def test_migration_reports_completion_and_v3_discard(tmp_path: Path) -> None:
    matrix = generate_csr("migrate", 987)
    v1_path = tmp_path / "v1.json"
    v2_path = tmp_path / "v2"
    v3_path = tmp_path / "v3.json"
    write_v1(v1_path, matrix)
    write_v2(v2_path, matrix)
    from truth_ledger.format_v3 import write_v3

    write_v3(v3_path, matrix)

    v1_to_v2 = convert_ledger(1, v1_path, 2, tmp_path / "converted-v2")
    v1_to_v3 = convert_ledger(1, v1_path, 3, tmp_path / "converted-v3.json")
    v3_to_v1 = convert_ledger(3, v3_path, 1, tmp_path / "v3-back-v1.json")
    assert v1_to_v2["semantic_equal"] is True
    assert any(item["field"] == "shard_boundaries" for item in v1_to_v2["completed"])
    assert any(item["field"] == "bitmap_digest" for item in v1_to_v2["completed"])
    assert any(
        item["field"] == "column_block_mapping" for item in v1_to_v3["completed"]
    )
    assert any(
        item["field"] == "structural_algebra_defaults"
        for item in v1_to_v3["completed"]
    )
    assert any("column_block" in item["rule"] for item in v3_to_v1["discarded"])
    assert any("bitmap" in item["rule"] for item in v3_to_v1["discarded"])
    assert any("column-block" in item["description"] for item in v3_to_v1["discarded"])


def test_write_receipt_marks_only_target_interval(case_root: Path) -> None:
    from truth_ledger.model import CSRMatrix

    initial = read_json(case_root / "initial_csr.json")
    current = CSRMatrix.from_json_dict(initial)
    event = read_json(case_root / "events" / "write-001.json")
    next_matrix, receipt = receive_write_batch(event, current)
    assert receipt["write_version"] == 1
    assert [item["interval_id"] for item in receipt["invalidated_intervals"]] == [
        "R01:3-6"
    ]
    assert receipt["affected_row_ranges"][0]["column_index_range"] is not None
    assert next_matrix.write_version == 1


def test_write_conflict_codes(case_root: Path) -> None:
    from truth_ledger.model import CSRMatrix

    current = CSRMatrix.from_json_dict(read_json(case_root / "initial_csr.json"))
    event = read_json(case_root / "events" / "write-001.json")

    bad_hash = json.loads(json.dumps(event))
    bad_hash["writes"][0]["content_hash"] = "sha256:" + "0" * 64
    with pytest.raises(LedgerError) as exc:
        receive_write_batch(bad_hash, current)
    assert exc.value.code == "E_WRITE_CONFLICT"
    assert exc.value.details["reason"] == "hash_mismatch"

    with pytest.raises(LedgerError) as exc:
        receive_write_batch(event, current, last_version=1)
    assert exc.value.code == "E_WRITE_CONFLICT"

    overlap = json.loads(json.dumps(event))
    overlap["writes"].append(dict(overlap["writes"][0]))
    with pytest.raises(LedgerError) as exc:
        receive_write_batch(overlap, current)
    assert exc.value.code == "E_WRITE_CONFLICT"


def test_pipeline_incremental_adjudication_and_self_check(case_root: Path) -> None:
    pipeline = Pipeline(case_root)
    first = pipeline.run_through(1)
    second = pipeline.run_through(2)
    assert first["ok"] is True
    assert second["recompute"]["recomputed_intervals"] == ["R02:6-9", "R03:9-12"]
    assert second["recompute"]["reused_intervals"] == ["R00:0-3", "R01:3-6"]
    report = adjudicate_from_disk(case_root)
    assert report["struct_equal"] is True
    assert report["numeric_equal"] is True
    check = run_self_check(case_root)
    assert check["spmm_elementwise_equal"] is True
    assert check["spmspm_elementwise_equal"] is True
    assert check["spmm_max_abs_diff"] == 0.0

    cli_check = subprocess.run(
        [
            sys.executable,
            "-m",
            "truth_ledger.cli",
            "adjudicate",
            "--case-dir",
            str(case_root),
        ],
        cwd=REPO_ROOT,
        env=_cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert cli_check.returncode == 0, cli_check.stderr


def test_sigkill_recovery_skips_committed_stages(tmp_path: Path) -> None:
    case_root = generate_sample_case(tmp_path / "recovery").root
    Pipeline(case_root).run_through(1)
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "truth_ledger.cli",
            "pipeline",
            "--case-dir",
            str(case_root),
            "--target-version",
            "2",
            "--crash-at",
            "recompute_after_first_new",
        ],
        cwd=REPO_ROOT,
        env=_cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert crashed.returncode == -9
    latest = read_json(case_root / "state" / "latest.json")
    assert latest["stage_index"] == 2
    assert latest["write_version"] == 2

    result = Pipeline(case_root).run_through(2)
    statuses = [(item["stage_index"], item["status"]) for item in result["stage_runs"]]
    assert statuses == [
        (1, "skipped_committed"),
        (2, "skipped_committed"),
        (3, "executed"),
        (4, "executed"),
    ]
    assert result["recovery_gc"]["removed_uncommitted_objects"] >= 1
    assert run_self_check(case_root)["ok"] is True


def test_sigkill_after_uncommitted_write_publish_rolls_back(tmp_path: Path) -> None:
    case_root = generate_sample_case(tmp_path / "write-recovery").root
    Pipeline(case_root).run_through(1)
    committed_before = read_json(case_root / "committed" / "csr.json")["content_hash"]
    crashed = subprocess.run(
        [
            sys.executable,
            "-m",
            "truth_ledger.cli",
            "pipeline",
            "--case-dir",
            str(case_root),
            "--target-version",
            "2",
            "--crash-at",
            "parse_after_publish_before_snapshot",
        ],
        cwd=REPO_ROOT,
        env=_cli_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert crashed.returncode == -9
    latest = read_json(case_root / "state" / "latest.json")
    assert latest["stage_index"] == 4
    assert latest["write_version"] == 1

    result = Pipeline(case_root).run_through(2)
    assert result["ok"] is True
    statuses = {(item["stage_index"], item["status"]) for item in result["stage_runs"]}
    assert (1, "executed") in statuses
    assert (2, "executed") in statuses
    assert read_json(case_root / "state" / "latest.json")["write_version"] == 2
    assert committed_before != read_json(case_root / "committed" / "csr.json")[
        "content_hash"
    ]
    assert run_self_check(case_root)["ok"] is True


def test_all_injection_business_codes(tmp_path: Path) -> None:
    summary = run_injection_suite(tmp_path / "injections")
    assert summary["ok"] is True
    observed = {record["name"]: record for record in summary["records"]}
    expected = {
        "struct_nonmonotonic_row_ptr": "E_STRUCT",
        "struct_column_oob": "E_STRUCT",
        "format_unknown_version": "E_VERSION_UNSUPPORTED",
        "format_row_field_count": "E_FORMAT",
        "format_index_dup": "E_INDEX_DUP",
        "format_shard_bound": "E_SHARD_BOUND",
        "format_bitmap_oob_or_cover": "E_BITMAP_COVER",
        "format_field_missing": "E_FIELD_MISSING",
        "format_unparseable_number": "E_PARSE",
        "write_hash_mismatch": "E_WRITE_CONFLICT",
        "recompute_column_oob": "E_STRUCT",
    }
    for name, code in expected.items():
        assert observed[name]["observed_code"] == code
        assert observed[name]["passed"] is True


def test_semantic_drift_reports_row_and_column() -> None:
    from truth_ledger.semantic import first_csr_diff

    left = generate_csr("diff", 321)
    right = generate_csr("diff", 321)
    right.values = right.values.copy()
    target_entry = int(left.row_ptr[1]) if int(left.row_ptr[1]) > 0 else 0
    right.values[target_entry] += 0.5
    diff = first_csr_diff(left, right)
    assert diff is not None
    assert diff["kind"] == "value"
    assert "row" in diff and "col" in diff
    with pytest.raises(LedgerError) as exc_info:
        assert_semantic_equal(left, right)
    assert exc_info.value.code == "E_SEMANTIC_DRIFT"
    assert exc_info.value.details["first_diff"]["row"] == diff["row"]
    assert exc_info.value.details["first_diff"]["col"] == diff["col"]


def test_tampered_cache_hash_is_cross_view_mismatch(case_root: Path) -> None:
    from truth_ledger.adjudicate import verify_view_from_disk
    from truth_ledger.cache import CacheStore
    from truth_ledger.workspace import CaseWorkspace

    Pipeline(case_root).run_through(1)
    ws = CaseWorkspace(case_root)
    verified = verify_view_from_disk(ws, 1)
    key = verified["intervals"][0]["cache_key"]
    cache = CacheStore(case_root / "cache")
    entry_path = cache.object_path(key)
    entry = read_json(entry_path)
    entry["spmm"]["contrib_digest"] = "sha256:" + "f" * 64
    write_json_atomic(entry_path, entry)

    with pytest.raises(LedgerError) as exc_info:
        adjudicate_from_disk(case_root)
    assert exc_info.value.code == "E_CROSS_VIEW_MISMATCH"


def test_bitmap_cover_error_from_sqlite(tmp_path: Path) -> None:
    matrix = generate_csr("bitmap", 654)
    v2_dir = tmp_path / "v2"
    write_v2(v2_dir, matrix)
    connection = sqlite3.connect(v2_dir / "bitmap_index.sqlite")
    connection.execute(
        "UPDATE bitmap_index SET bitmap = ? WHERE shard_id = 0",
        (sqlite3.Binary(b"\x00"),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError) as exc:
        read_v2(v2_dir)
    assert exc.value.code == "E_BITMAP_COVER"
