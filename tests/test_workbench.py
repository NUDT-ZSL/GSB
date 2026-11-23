from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import threading
import unittest

from density_workbench import BusinessError, Workbench, ensure_workspace
from density_workbench.canonical import canonical_dumps, sha256_obj, write_json_atomic
from density_workbench.config import WorkbenchConfig
from density_workbench.convergence import decide_convergence
from density_workbench.data import deterministic_samples
from density_workbench.spectrum import recompute_stage


class WorkbenchTestCase(unittest.TestCase):
    n_samples = 24

    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="density-workbench-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.paths = ensure_workspace(
            self.root, n_samples=self.n_samples, dimensions=3
        )
        self.config = WorkbenchConfig.from_json_file(str(self.paths["config"]))
        self.config.chunk_size = 5
        self.config.max_iter = 20
        self.config.latency_budget_ms = 1_000.0
        write_json_atomic(
            self.paths["config"], self.config.to_dict(), indent=2
        )
        self.workbench = Workbench(self.root)


class DeterminismAndConsistencyTests(WorkbenchTestCase):
    def test_gmm_reproducibility_and_query_batch_consistency(self) -> None:
        first = self.workbench.analyze(force=True)
        first_rows = canonical_dumps(self.workbench.state.batch_payload["rows"])
        first_trace = canonical_dumps(self.workbench.state.batch_payload["ll_trace"])
        second = self.workbench.analyze(force=True)
        second_rows = canonical_dumps(self.workbench.state.batch_payload["rows"])
        second_trace = canonical_dumps(self.workbench.state.batch_payload["ll_trace"])
        self.assertEqual(first["batch_checksum"], second["batch_checksum"])
        self.assertEqual(first_rows, second_rows)
        self.assertEqual(first_trace, second_trace)
        for sample_id in ("sample-0000", "sample-0007", "sample-0023"):
            result = self.workbench.query(sample_id)
            row = self.workbench.query_index[sample_id]
            self.assertTrue(result["consistency"]["consistent"])
            self.assertEqual(result["component"], row["component"])
            self.assertEqual(result["density"], row["density"])
            self.assertEqual(result["responsibilities"], row["responsibilities"])
            self.assertEqual(result["result_checksum"], row["result_checksum"])
            self.assertGreaterEqual(result["latency"]["elapsed_ms"], 0.0)
            self.assertIn("kernel_evaluations", result["estimate"])
            self.assertIn("sample_count", result["estimate"])
            self.assertIn("chunk_count", result["estimate"])

    def test_float32_and_fixed_modes_share_query_batch_results(self) -> None:
        for precision in ("float32", "fixed"):
            with self.subTest(precision=precision):
                self.workbench.reconfigure(precision=precision, chunk_size=4)
                analysis = self.workbench.analyze(force=True)
                self.assertEqual(analysis["precision"], precision)
                result = self.workbench.query(
                    "sample-0005", latency_budget_ms=2_000.0
                )
                row = self.workbench.query_index["sample-0005"]
                self.assertEqual(result["density"], row["density"])
                self.assertEqual(
                    result["responsibilities"], row["responsibilities"]
                )
                self.assertLessEqual(
                    result["consistency"]["max_absolute_deviation"],
                    result["consistency"]["tolerance"],
                )
                if precision == "fixed":
                    self.assertEqual(
                        row["density"], round(row["density"] * 1_000_000) / 1_000_000
                    )

    def test_kde_query_batch_and_latency_estimate(self) -> None:
        self.workbench.reconfigure(
            model_family="kde", bandwidth=0.7, chunk_size=4
        )
        self.workbench.analyze(force=True)
        result = self.workbench.query("sample-0011", latency_budget_ms=2_000.0)
        row = self.workbench.query_index["sample-0011"]
        self.assertEqual(result["model_family"], "kde")
        self.assertEqual(result["responsibilities"], [])
        self.assertIsNone(result["component"])
        self.assertEqual(result["density"], row["density"])
        self.assertEqual(result["result_checksum"], row["result_checksum"])
        self.assertEqual(
            result["estimate"]["kernel_evaluations"], self.n_samples
        )
        self.assertEqual(
            result["estimate"]["chunk_count"],
            (self.n_samples + 3) // 4,
        )
        self.assertTrue(result["consistency"]["consistent"])

    def test_error_spectrum_recomputes_and_localizes(self) -> None:
        self.workbench.reconfigure(precision="float32", chunk_size=4)
        self.workbench.analyze(force=True)
        spectrum = self.workbench.error_spectrum()
        self.assertEqual(spectrum["reference_precision"], "float64")
        self.assertEqual(spectrum["candidate_precision"], "float32")
        self.assertIn(
            spectrum["localization"]["dominant_source"],
            {
                "chunk_reduction",
                "quantization_rounding",
                "convergence_decision",
            },
        )
        stage_names = {stage["stage"] for stage in spectrum["stages"]}
        self.assertEqual(
            stage_names,
            {
                "standardization",
                "chunk_reduction",
                "quantization_rounding",
                "convergence_decision",
            },
        )
        for stage in spectrum["stages"]:
            self.assertEqual(recompute_stage(stage), stage["summary"])
        self.assertTrue(self.workbench.self_check()["ok"])

    def test_fixed_error_spectrum_identifies_quantization(self) -> None:
        self.workbench.reconfigure(precision="fixed", chunk_size=8)
        self.workbench.analyze(force=True)
        spectrum = self.workbench.error_spectrum()
        quant_stage = next(
            stage
            for stage in spectrum["stages"]
            if stage["stage"] == "quantization_rounding"
        )
        self.assertGreater(
            quant_stage["summary"]["max_absolute_deviation"], 0.0
        )
        self.assertTrue(self.workbench.self_check()["ok"])

    def test_near_threshold_convergence_round_is_stable_across_precisions(self) -> None:
        iterations = []
        for precision in ("float64", "float32", "fixed"):
            self.workbench.reconfigure(precision=precision, chunk_size=5)
            self.workbench.analyze(force=True)
            iterations.append(
                self.workbench.state.batch_payload["convergence"]["iteration"]
            )
        self.assertEqual(len(set(iterations)), 1, iterations)


class CacheAndInvalidationTests(WorkbenchTestCase):
    def test_identical_analysis_reuses_content_addressed_snapshot(self) -> None:
        first = self.workbench.analyze(force=True)
        second = self.workbench.analyze(force=False)
        self.assertEqual(first["batch_checksum"], second["batch_checksum"])
        self.assertEqual(
            first["snapshot"]["bundle_version"],
            second["snapshot"]["bundle_version"],
        )
        self.assertEqual(second["invalidation"]["snapshot_action"], "reused")
        self.assertEqual(second["invalidation"]["recomputed_samples"], [])
        self.assertEqual(
            second["invalidation"]["reused_samples"],
            self.workbench.sample_ids,
        )
        self.assertGreater(second["cache"]["hit_count"], 0)

    def test_sample_change_bumps_snapshot_and_partitions_invalidation(self) -> None:
        first = self.workbench.analyze(force=True)
        old_version = first["snapshot"]["bundle_version"]
        self.workbench.upsert_sample(
            {
                "sample_id": "sample-0003",
                "feature": [9.25, -4.5, 2.75],
                "group": "modified",
            }
        )
        changed = self.workbench.analyze(force=False)
        self.assertEqual(
            changed["snapshot"]["bundle_version"], old_version + 1
        )
        invalidation = changed["invalidation"]
        self.assertIn("sample-0003", invalidation["changed_samples"])
        self.assertEqual(invalidation["recomputed_samples"], self.workbench.sample_ids)
        self.assertEqual(invalidation["reused_samples"], [])
        self.assertFalse(
            set(invalidation["recomputed_samples"])
            & set(invalidation["reused_samples"])
        )
        self.assertTrue(self.workbench.self_check()["ok"])

    def test_chunk_and_precision_change_invalidate_batch_key(self) -> None:
        first = self.workbench.analyze(force=True)
        self.workbench.reconfigure(chunk_size=3)
        second = self.workbench.analyze(force=False)
        self.assertNotEqual(first["batch_checksum"], second["batch_checksum"])
        self.workbench.reconfigure(precision="float32")
        third = self.workbench.analyze(force=False)
        self.assertNotEqual(second["batch_checksum"], third["batch_checksum"])
        self.assertGreater(third["snapshot"]["bundle_version"], 1)


class SnapshotAndConcurrencyTests(WorkbenchTestCase):
    def test_snapshot_rollback_is_rejected(self) -> None:
        analysis = self.workbench.analyze(force=True)
        version = analysis["snapshot"]["bundle_version"]
        payloads = {
            "statistics": {"x": 1},
            "model": {"x": 1},
            "batch": {"x": 1},
        }
        with self.assertRaises(BusinessError) as caught:
            self.workbench.snapshots.commit_bundle(
                payloads, requested_version=version
            )
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(
            caught.exception.details["requested_version"], version
        )

    def test_concurrent_commit_returns_stage_busy_immediately(self) -> None:
        self.workbench.analyze(force=True)
        with self.workbench.snapshots.commit_guard():
            with self.assertRaises(BusinessError) as caught:
                self.workbench.snapshots.commit_bundle(
                    {
                        "statistics": {"x": 2},
                        "model": {"x": 2},
                        "batch": {"x": 2},
                    }
                )
        self.assertEqual(caught.exception.code, "E_STAGE_BUSY")

    def test_corrupt_snapshot_reports_category_and_version(self) -> None:
        self.workbench.analyze(force=True)
        snapshot_root = self.root / "snapshots"
        manifest = json.loads(
            (snapshot_root / "current.json").read_text(encoding="utf-8")
        )
        bundle_path = snapshot_root / manifest["bundle_file"]
        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        bundle["categories"]["model"]["payload"]["tampered"] = True
        bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
        with self.assertRaises(BusinessError) as caught:
            Workbench(self.root)
        error = caught.exception
        self.assertEqual(error.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(error.details["category"], "model")
        self.assertEqual(error.details["version"], 1)

    def test_restart_hydrates_matching_snapshot_without_new_version(self) -> None:
        first = self.workbench.analyze(force=True)
        restarted = Workbench(self.root)
        self.assertIsNotNone(restarted.state)
        result = restarted.query("sample-0002", latency_budget_ms=2_000.0)
        self.assertTrue(result["consistency"]["consistent"])
        self.assertEqual(
            restarted.snapshot_status()["bundle_version"],
            first["snapshot"]["bundle_version"],
        )


class ErrorAndResourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="density-workbench-errors-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.samples = deterministic_samples(n_samples=8, dimensions=3)
        self.config = WorkbenchConfig(chunk_size=3, max_iter=10)

    def test_input_and_config_business_codes(self) -> None:
        duplicate = [dict(item) for item in self.samples]
        duplicate[1]["sample_id"] = duplicate[0]["sample_id"]
        with self.assertRaises(BusinessError) as duplicate_error:
            Workbench(self.root / "d1", config=self.config, samples=duplicate)
        self.assertEqual(
            duplicate_error.exception.code, "E_DUPLICATE_SAMPLE_ID"
        )

        ragged = [dict(item) for item in self.samples]
        ragged[1]["feature"] = [1.0, 2.0]
        with self.assertRaises(BusinessError) as dimension_error:
            Workbench(self.root / "d2", config=self.config, samples=ragged)
        self.assertEqual(dimension_error.exception.code, "E_FEATURE_DIM")

        with self.assertRaises(BusinessError) as component_error:
            Workbench(
                self.root / "d3",
                config={"n_components": 0},
                samples=self.samples,
            )
        self.assertEqual(component_error.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(component_error.exception.details["field"], "n_components")

        with self.assertRaises(BusinessError) as bandwidth_error:
            Workbench(
                self.root / "d4",
                config={"model_family": "kde", "bandwidth": 0.0},
                samples=self.samples,
            )
        self.assertEqual(bandwidth_error.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(bandwidth_error.exception.details["field"], "bandwidth")

        with self.assertRaises(BusinessError) as precision_error:
            Workbench(
                self.root / "d5",
                config={"precision": "float16"},
                samples=self.samples,
            )
        self.assertEqual(
            precision_error.exception.code, "E_PRECISION_UNKNOWN"
        )

        with self.assertRaises(BusinessError) as tolerance_error:
            Workbench(
                self.root / "d6",
                config={"tol": 0.0},
                samples=self.samples,
            )
        self.assertEqual(tolerance_error.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(tolerance_error.exception.details["field"], "tol")

    def test_resource_limits_and_latency_budget(self) -> None:
        batch_limited = WorkbenchConfig(
            chunk_size=3, max_batch_samples=4, max_iter=5
        )
        workbench = Workbench(
            self.root / "resource-batch",
            config=batch_limited,
            samples=self.samples,
        )
        with self.assertRaises(BusinessError) as batch_error:
            workbench.analyze()
        self.assertEqual(batch_error.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            batch_error.exception.details["category"], "max_batch_samples"
        )

        memory_limited = WorkbenchConfig(
            chunk_size=3,
            max_responsibility_memory_mb=0.000001,
            max_iter=5,
        )
        workbench = Workbench(
            self.root / "resource-memory",
            config=memory_limited,
            samples=self.samples,
        )
        with self.assertRaises(BusinessError) as memory_error:
            workbench.analyze()
        self.assertEqual(memory_error.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            memory_error.exception.details["category"],
            "max_responsibility_memory_mb",
        )

        budgeted = WorkbenchConfig(
            chunk_size=3, latency_budget_ms=1_000.0, max_iter=5
        )
        workbench = Workbench(
            self.root / "latency", config=budgeted, samples=self.samples
        )
        workbench.analyze(force=True)
        with self.assertRaises(BusinessError) as latency_error:
            workbench.query("sample-0000", latency_budget_ms=1e-9)
        self.assertEqual(latency_error.exception.code, "E_LATENCY_BUDGET")
        details = latency_error.exception.details
        self.assertIn("completed_chunks", details)
        self.assertIn("consumed_budget_ms", details)
        self.assertGreater(details["consumed_budget_ms"], 0.0)
        self.assertEqual(
            details["timing_source"],
            "time.perf_counter_ns around actual query path "
            "(state resolution and compute loops)",
        )

    def test_inconsistent_query_reports_first_deviation(self) -> None:
        workbench = Workbench(
            self.root / "inconsistent",
            config=WorkbenchConfig(chunk_size=3, max_iter=5),
            samples=self.samples,
        )
        workbench.analyze(force=True)
        sample_id = "sample-0001"
        workbench.query_index[sample_id]["responsibilities"][0] += 1.0
        with self.assertRaises(BusinessError) as caught:
            workbench.query(sample_id, latency_budget_ms=2_000.0)
        error = caught.exception
        self.assertEqual(error.code, "E_INCONSISTENT")
        self.assertIn("first_deviation", error.details)
        self.assertIn("query_intermediate", error.details)
        self.assertIn("batch_intermediate", error.details)

    def test_parallel_query_limit(self) -> None:
        config = WorkbenchConfig(
            chunk_size=3, max_parallel_queries=1, max_iter=5
        )
        workbench = Workbench(
            self.root / "parallel", config=config, samples=self.samples
        )
        workbench.analyze(force=True)
        with workbench._query_slot():
            with self.assertRaises(BusinessError) as caught:
                workbench.query("sample-0000", latency_budget_ms=2_000.0)
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(
            caught.exception.details["category"], "max_parallel_queries"
        )


class ConvergenceProtectionTests(unittest.TestCase):
    def test_threshold_band_prevents_precision_flip(self) -> None:
        tol = 1e-7
        for epsilon in (2.220446049250313e-16, 1.1920928955078125e-07, 1e-6):
            decisions = []
            for delta in (-1e-12, 0.0, 1e-12):
                decisions.append(
                    decide_convergence(
                        iteration=5,
                        improvement=tol + delta,
                        tol=tol,
                        min_improvement=1e-10,
                        policy="tolerance_band",
                        rel_band=1e-6,
                        abs_band=1e-10,
                        numerical_epsilon=epsilon,
                    )
                )
            self.assertTrue(all(item.protected for item in decisions))
            self.assertTrue(all(item.converged for item in decisions))
            self.assertEqual(len({item.converged for item in decisions}), 1)

    def test_invalid_threshold_field_is_reported(self) -> None:
        with self.assertRaises(BusinessError) as caught:
            decide_convergence(
                iteration=1,
                improvement=1e-8,
                tol=1e-7,
                min_improvement=-1.0,
                policy="strict",
                rel_band=0.0,
                abs_band=0.0,
            )
        self.assertEqual(caught.exception.code, "E_CONFIG_INVALID")
        self.assertEqual(
            caught.exception.details["field"], "min_improvement"
        )


if __name__ == "__main__":
    unittest.main()
