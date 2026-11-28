from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sparse_kernel_workbench.audit import (
    ledger_hash,
    recommend_strategy,
)
from sparse_kernel_workbench.pipeline import atomic_write_json, build_report
from sparse_kernel_workbench.spec import load_operation_spec


ROOT = Path(__file__).resolve().parents[1]
OPERATION = ROOT / "data" / "operation_spec.json"
EXPECTED_EMPTY_ROWS = [12, 17, 22, 29, 32, 33]
EXPECTED_ROWS = 48
EXPECTED_COLUMNS = 24
EXPECTED_NONZEROS = 88


class WorkbenchIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.report = build_report(
            operation_spec=OPERATION,
            base_dir=ROOT,
        )

    def test_report_is_successful_and_deterministic(self) -> None:
        self.assertEqual(self.report["status"], "ok")
        self.assertEqual(len(self.report["versions"]), 3)
        self.assertTrue(self.report["cross_version"]["consistent"])
        self.assertEqual(self.report["recommendation"]["recommended_layout"], "csr")
        second = build_report(operation_spec=OPERATION, base_dir=ROOT)
        self.assertEqual(
            self.report["run_fingerprint"],
            second["run_fingerprint"],
        )

    def test_normalization_is_coordinate_and_weight_equivalent(self) -> None:
        hashes = set()
        for version in self.report["versions"]:
            normalization = version["normalization"]
            self.assertEqual(
                [normalization["normalized_rows"], normalization["normalized_columns"]],
                [EXPECTED_ROWS, EXPECTED_COLUMNS],
            )
            self.assertEqual(
                normalization["canonical_nonzero_count"],
                EXPECTED_NONZEROS,
            )
            self.assertEqual(normalization["empty_rows"], EXPECTED_EMPTY_ROWS)
            hashes.add(version["fingerprints"]["matrix"])
        self.assertEqual(len(hashes), 1)

        expected = {
            "v1-matrix-market-1based-count": ("1", -1),
            "v2-csr-0based-permille": ("0.001", 0),
            "v3-csc-1based-basis-points": ("0.0001", -1),
        }
        for version in self.report["versions"]:
            normalization = version["normalization"]
            factor, offset = expected[version["version"]]
            self.assertEqual(normalization["weight_conversion_factor"], factor)
            self.assertEqual(normalization["index_offset_to_zero"], offset)
            corrections = normalization["field_corrections"]
            self.assertTrue(
                any(
                    item.get("kind") == "index_offset"
                    and item.get("offset") == offset
                    for item in corrections
                )
            )
            self.assertTrue(
                any(
                    item.get("kind") == "weight_unit"
                    and item.get("factor") == factor
                    for item in corrections
                )
            )
        v2_corrections = self.report["versions"][1]["normalization"][
            "field_corrections"
        ]
        v3_corrections = self.report["versions"][2]["normalization"][
            "field_corrections"
        ]
        self.assertTrue(
            any(
                item.get("kind") == "storage_field_name"
                and item.get("raw_field") == "item_indices"
                for item in v2_corrections
            )
        )
        self.assertTrue(
            any(
                item.get("kind") == "storage_field_name"
                and item.get("raw_field") == "user_row_indices"
                for item in v3_corrections
            )
        )

    def test_reference_histogram_and_tolerance(self) -> None:
        for version in self.report["versions"]:
            reference = version["reference"]
            self.assertEqual(reference["matrix_shape"], [EXPECTED_ROWS, EXPECTED_COLUMNS])
            self.assertEqual(len(reference["vector"]), EXPECTED_ROWS)
            for strategy in version["strategies"].values():
                comparison = strategy["comparison"]
                self.assertTrue(comparison["within_tolerance"])
                self.assertEqual(comparison["exceeded_elements"], [])
                self.assertLess(comparison["max_abs_error"], 1e-9)
                histogram = comparison["error_histogram"]
                self.assertEqual(sum(histogram["counts"]), EXPECTED_ROWS)
                self.assertEqual(histogram["bin_count"], 8)
                self.assertEqual(len(histogram["bin_edges"]), 9)

    def test_row_safe_batches_and_explicit_empty_rows(self) -> None:
        for version in self.report["versions"]:
            for layout, strategy in version["strategies"].items():
                batching = strategy["batching"]
                ledger = strategy["ledger"]
                self.assertEqual(batching["batch_count"], 7)
                self.assertEqual(ledger["batch_count"], 7)
                self.assertEqual(ledger["nonzero_scan_total"], EXPECTED_NONZEROS)
                self.assertEqual(ledger["mean_nonzeros_per_result_element"], 88 / 48)
                self.assertEqual(ledger["hotspot_batch_ids"], [0])
                self.assertEqual(batching["empty_rows"], EXPECTED_EMPTY_ROWS)
                self.assertTrue(
                    batching["row_integrity"]["every_row_in_exactly_one_batch"]
                )

                first = batching["batches"][0]
                self.assertEqual(first["row_interval"], [0, 1])
                self.assertEqual(first["nonzero_count"], 24)
                self.assertTrue(first["over_budget"])
                self.assertEqual(
                    first["reason"],
                    "single_row_exceeds_batch_budget_kept_whole",
                )

                intervals = [
                    batch["canonical_nonzero_interval"]
                    for batch in batching["batches"]
                ]
                cursor = 0
                for start, end in intervals:
                    self.assertEqual(start, cursor)
                    cursor = end
                self.assertEqual(cursor, EXPECTED_NONZEROS)

                explicit_empty = {
                    row
                    for batch in batching["batches"]
                    for row in batch["empty_rows"]
                }
                self.assertEqual(sorted(explicit_empty), EXPECTED_EMPTY_ROWS)
                self.assertTrue(
                    ledger["scan_accounting"]["every_canonical_nonzero_scanned_once"]
                )
                self.assertEqual(ledger["scan_accounting"]["duplicate_scans"], 0)
                self.assertEqual(ledger["scan_accounting"]["missing_scans"], 0)
                self.assertTrue(
                    ledger["result_ownership"][
                        "every_result_element_owned_by_exactly_one_batch"
                    ]
                )
                self.assertEqual(
                    set(ledger["result_ownership"]["batch_count_by_result_element"]),
                    {1},
                )
                for batch in ledger["batches"]:
                    accesses = sum(
                        item["accesses"]
                        for item in batch["dense_vector_chunk_reuse"]
                    )
                    hits = sum(
                        item["hits"] for item in batch["dense_vector_chunk_reuse"]
                    )
                    loads = sum(
                        item["loads"] for item in batch["dense_vector_chunk_reuse"]
                    )
                    self.assertEqual(accesses, batch["scanned_nonzero_count"])
                    self.assertEqual(hits, batch["dense_chunk_hits"])
                    self.assertEqual(loads, batch["dense_chunk_misses"])

    def test_two_layouts_are_compared_and_ledgers_are_cross_version_stable(self) -> None:
        for version in self.report["versions"]:
            self.assertEqual(set(version["strategies"]), {"csr", "csc"})
            self.assertTrue(
                version["recommendation"]["pairwise_comparisons"]["csr_vs_csc"][
                    "consistent"
                ]
            )
            self.assertEqual(
                version["fingerprints"]["matrix"],
                self.report["versions"][0]["fingerprints"]["matrix"],
            )
        for layout in ("csr", "csc"):
            hashes = {
                ledger_hash(version["strategies"][layout]["ledger"])
                for version in self.report["versions"]
            }
            self.assertEqual(len(hashes), 1)

    def test_direction_override_changes_layout_selection(self) -> None:
        report = build_report(
            operation_spec=OPERATION,
            base_dir=ROOT,
            direction="column_accumulate",
        )
        self.assertEqual(report["status"], "ok")
        for version in report["versions"]:
            self.assertEqual(version["layout_selection"]["selected"], "csc")
        self.assertEqual(report["recommendation"]["recommended_layout"], "csc")

    def test_atomic_success_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "nested" / "report.json"
            atomic_write_json(output, self.report)
            loaded = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(loaded["run_fingerprint"], self.report["run_fingerprint"])
            self.assertEqual(loaded["status"], "ok")

    def test_bad_index_reports_business_error_and_writes_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = ROOT / "data" / "versions" / "v2_csr_0based_permille"
            bad_dir = temporary_path / "versions" / "bad_csr"
            shutil.copytree(source, bad_dir)
            matrix_path = bad_dir / "matrix.csr"
            lines = matrix_path.read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                if line.startswith("item_indices "):
                    tokens = line.split()
                    tokens[1] = "99"
                    lines[index] = " ".join(tokens)
                    break
            matrix_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            spec_path = bad_dir / "version.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["version"] = "bad-csr-index"
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            output = temporary_path / "report.json"
            command = [
                sys.executable,
                "-m",
                "sparse_kernel_workbench",
                "run",
                "--base-dir",
                str(ROOT),
                "--operation",
                str(OPERATION),
                "--version-spec",
                str(spec_path),
                "--output",
                str(output),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertFalse(output.exists())
            error = json.loads(completed.stderr)
            self.assertEqual(error["error"]["code"], "SKW-NORM-002")
            context = error["error"]["context"]
            self.assertEqual(context["element"], "nnz[0]")
            self.assertEqual(context["field"], "item_indices")
            self.assertEqual(context["raw_value"], 99)

    def test_cross_version_weight_mismatch_refuses_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            temporary_path = Path(temporary)
            source = ROOT / "data" / "versions" / "v2_csr_0based_permille"
            bad_dir = temporary_path / "bad_weight"
            shutil.copytree(source, bad_dir)
            spec_path = bad_dir / "version.json"
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
            spec["version"] = "v2-bad-weight"
            spec["weight"]["factor"] = "2"
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            report = build_report(
                operation_spec=OPERATION,
                version_specs=[
                    ROOT / "data" / "versions" / "v1_matrix_market_1based" / "version.json",
                    spec_path,
                ],
                base_dir=ROOT,
            )
            self.assertEqual(report["status"], "conflict")
            self.assertFalse(report["cross_version"]["consistent"])
            self.assertIsNone(report["recommendation"])
            self.assertTrue(report["recommendation_refused"])

    def test_strategy_conflict_has_no_recommendation(self) -> None:
        operation = load_operation_spec(OPERATION)
        ledger = {
            "nonzero_scan_total": 2,
            "batch_count": 2,
            "hotspot_batch_ids": [],
            "hotspot_batch_count": 0,
            "hotspot_scan_total": 0,
            "peak_batch_nonzeros": 1,
            "dense_chunk_hits": 0,
        }
        runs = {
            "csr": {
                "result_vector": [1.0, 0.0],
                "ledger": dict(ledger),
                "comparison": {"within_tolerance": True},
            },
            "csc": {
                "result_vector": [2.0, 0.0],
                "ledger": dict(ledger),
                "comparison": {"within_tolerance": True},
            },
        }
        recommendation, conflict = recommend_strategy(operation, "csr", runs)
        self.assertIsNone(recommendation)
        self.assertIsNotNone(conflict)
        assert conflict is not None
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(
            conflict["conflicts"][0]["kind"],
            "strategy_result_conflict",
        )

    def test_matrix_fingerprint_is_shared_by_all_versions(self) -> None:
        hashes = set()
        for version in self.report["versions"]:
            hashes.add(version["fingerprints"]["matrix"])
        self.assertEqual(len(hashes), 1)


if __name__ == "__main__":
    unittest.main()
