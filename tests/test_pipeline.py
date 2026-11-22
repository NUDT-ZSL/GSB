from __future__ import annotations

import shutil
import tempfile
import time
import multiprocessing
import unittest
from pathlib import Path
from unittest.mock import patch

from ocrlab.config import PipelineConfig
from ocrlab.errors import (
    BusinessError,
    E_DUPLICATE_PAGE_ID,
    E_IMAGE_SHAPE_MISMATCH,
    E_INCONSISTENT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_TOLERANCE_EXCEEDED,
    E_UNKNOWN_CHARSET_VERSION,
    SimulatedCrash,
)
from ocrlab.pipeline import PipelineRunner
from ocrlab.samples import generate_workspace, load_manifest, validate_manifest
from ocrlab.serialization import atomic_write_json, checksum, read_json
from ocrlab.snapshots import CommitGuard, SnapshotStore
from ocrlab.templates import get_template_library


class PipelineAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_root = Path(tempfile.mkdtemp(prefix="ocrlab-tests-"))
        cls.data_dir = cls.temp_root / "data"
        cls.records = generate_workspace(
            cls.data_dir,
            page_count=6,
            base_seed=20261123,
            force=True,
        )
        cls.library = get_template_library("glyph-v1")

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.temp_root, ignore_errors=True)

    def state(self, name: str) -> Path:
        return self.temp_root / name

    def test_deterministic_pipeline_stage_sets_and_reference(self) -> None:
        first = PipelineRunner(self.data_dir, self.state("det-a")).run([], reset=True)
        second = PipelineRunner(self.data_dir, self.state("det-b")).run([], reset=True)
        self.assertEqual(first.report_bytes, second.report_bytes)
        self.assertEqual(first.injection_log_bytes, second.injection_log_bytes)
        for stage in range(1, 5):
            self.assertEqual(
                (self.state("det-a") / f"stage_{stage}.json").read_bytes(),
                (self.state("det-b") / f"stage_{stage}.json").read_bytes(),
            )
        self.assertTrue(first.selfcheck["ok"])
        expected = [record["page_id"] for record in self.records]
        for stage in range(1, 5):
            self.assertEqual(first.report["stage_page_sets"][str(stage)], expected)
        for page in first.report["pages"]:
            self.assertEqual(page["recognized_text"], page["truth_text"])
            self.assertEqual(page["reference_text"], page["truth_text"])
            self.assertTrue(page["comparison"]["ok"])
            self.assertEqual(page["comparison"]["max_coordinate_deviation"], 0.0)
            self.assertGreater(page["metrics"]["mean_confidence"], 0.9)
        self.assertEqual(first.report["config"]["checksum"], PipelineConfig().config_checksum)
        self.assertEqual(
            first.report["template_library"]["checksum"],
            self.library.checksum,
        )
        for _, metadata in first.snapshot_metadata["stages"].items():
            self.assertEqual(metadata["config_version"], first.report["config"]["version"])
            self.assertEqual(metadata["config_checksum"], first.report["config"]["checksum"])
            self.assertEqual(metadata["template_version"], "glyph-v1")
            self.assertEqual(metadata["template_checksum"], self.library.checksum)
            self.assertEqual(metadata["dataset_checksum"], first.report["dataset_checksum"])

    def test_stage_line_boxes_are_tie_sorted_by_y_then_x(self) -> None:
        result = PipelineRunner(self.data_dir, self.state("sort")).run([], reset=True)
        store = SnapshotStore(self.state("sort"))
        stage_two = store.load(2, expected_pages={r["page_id"] for r in self.records})
        assert stage_two is not None
        for page in stage_two.content["pages"].values():
            boxes = page["boxes"]
            keys = [(-box["score"], box["y0"], box["x0"]) for box in boxes]
            self.assertEqual(keys, sorted(keys))
        self.assertTrue(result.selfcheck["ok"])

    def test_all_four_injection_stages_are_attributed_and_deterministic(self) -> None:
        pages = load_manifest(self.data_dir)
        injections = [
            {
                "stage": "preprocess",
                "code": "E_PIXEL_OUT_OF_RANGE",
                "page_id": pages[0]["page_id"],
                "params": {"value": 999, "x": 3, "y": 4},
            },
            {
                "stage": "detect",
                "code": "E_INVALID_LINE_BOX",
                "page_id": pages[1]["page_id"],
                "line_no": 1,
                "params": {"box": [-8, -4, 9000, 20]},
            },
            {
                "stage": "recognize",
                "code": "E_TEMPLATE_SHAPE_MISMATCH",
                "page_id": pages[2]["page_id"],
                "line_no": 2,
                "params": {"shape": [3, 3], "expected": [21, 15]},
            },
            {
                "stage": "aggregate",
                "code": "E_INVALID_THRESHOLD",
                "page_id": pages[3]["page_id"],
                "params": {"threshold": 0},
            },
        ]
        first = PipelineRunner(self.data_dir, self.state("inj-a")).run(injections, reset=True)
        second = PipelineRunner(self.data_dir, self.state("inj-b")).run(injections, reset=True)
        self.assertEqual(first.injection_log_bytes, second.injection_log_bytes)
        self.assertEqual(first.report_bytes, second.report_bytes)
        self.assertEqual(
            set(first.report["business_codes"]),
            {
                "E_PIXEL_OUT_OF_RANGE",
                "E_INVALID_LINE_BOX",
                "E_TEMPLATE_SHAPE_MISMATCH",
                "E_INVALID_THRESHOLD",
            },
        )
        self.assertEqual(len(first.injection_log), 4)
        for record in first.injection_log:
            self.assertIn("stage", record)
            self.assertIn("code", record)
            self.assertIn("page_id", record)
            self.assertIn("line_no", record)
        by_page = {page["page_id"]: page for page in first.report["pages"]}
        self.assertEqual(by_page[pages[0]["page_id"]]["business_codes"], ["E_PIXEL_OUT_OF_RANGE"])
        categories = {
            item["category"]
            for item in by_page[pages[0]["page_id"]]["degradations"]
        }
        self.assertIn("truth_box_layout", categories)
        self.assertEqual(
            [
                item["category"]
                for item in by_page[pages[1]["page_id"]]["degradations"]
            ],
            ["truth_box_layout"],
        )
        self.assertEqual(
            by_page[pages[2]["page_id"]]["degradations"][0]["category"],
            "low_resolution_templates",
        )
        self.assertEqual(
            by_page[pages[3]["page_id"]]["degradations"][0]["category"],
            "default_aggregation_threshold",
        )
        for page in first.report["pages"]:
            self.assertGreater(len(page["recognized_text"]), 0)
            self.assertTrue(page["comparison"]["ok"])

    def test_forced_termination_recovery_matches_uninterrupted_bytes(self) -> None:
        state = self.state("recovery")
        baseline = PipelineRunner(self.data_dir, state).run([], reset=True)
        with self.assertRaises(SimulatedCrash):
            PipelineRunner(self.data_dir, state).run([], reset=True, crash_after_stage=2)
        resumed = PipelineRunner(self.data_dir, state).run([])
        self.assertEqual(resumed.report_bytes, baseline.report_bytes)
        self.assertEqual(resumed.runtime["resumed_from_stage"], 3)
        self.assertEqual(set(resumed.runtime["reused_pages_by_stage"]), {"1", "2"})
        self.assertEqual(set(resumed.runtime["recomputed_pages_by_stage"]), {"3", "4"})
        expected = {record["page_id"] for record in self.records}
        for stage in ("1", "2"):
            self.assertEqual(set(resumed.runtime["reused_pages_by_stage"][stage]), expected)
        self.assertTrue(resumed.selfcheck["ok"])

    def test_injected_run_recovers_to_identical_report_and_log(self) -> None:
        pages = load_manifest(self.data_dir)
        injections = [
            {
                "stage": "detect",
                "code": "E_INVALID_LINE_BOX",
                "page_id": pages[1]["page_id"],
                "line_no": 1,
                "params": {"box": [-2, -2, 10000, 20]},
            }
        ]
        baseline = PipelineRunner(self.data_dir, self.state("inj-rec-baseline")).run(
            injections,
            reset=True,
        )
        state = self.state("inj-rec")
        with self.assertRaises(SimulatedCrash):
            PipelineRunner(self.data_dir, state).run(
                injections,
                reset=True,
                crash_after_stage=2,
            )
        resumed = PipelineRunner(self.data_dir, state).run(injections)
        self.assertEqual(resumed.report_bytes, baseline.report_bytes)
        self.assertEqual(resumed.injection_log_bytes, baseline.injection_log_bytes)

    def test_snapshot_versions_monotonic_rollback_and_corruption_codes(self) -> None:
        state = self.state("snapshots")
        runner = PipelineRunner(self.data_dir, state)
        first = runner.run([], reset=True)
        versions = {
            int(stage): metadata["version"]
            for stage, metadata in first.snapshot_metadata["stages"].items()
        }
        self.assertEqual(versions, {1: 1, 2: 2, 3: 3, 4: 4})
        with self.assertRaises(BusinessError) as caught:
            runner.store.commit_stage(
                1,
                stage_name="preprocess",
                signature="rollback-test",
                content={"pages": {record["page_id"]: {"page_id": record["page_id"]} for record in self.records}},
                expected_pages={record["page_id"] for record in self.records},
                explicit_version=1,
            )
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)
        second = runner.run([], reset=True)
        for stage in range(1, 5):
            self.assertGreater(
                second.snapshot_metadata["stages"][str(stage)]["version"],
                versions[stage],
            )
        corrupt_state = self.state("corrupt")
        shutil.copytree(state, corrupt_state)
        snapshot_path = corrupt_state / "stage_3.json"
        payload = read_json(snapshot_path)
        payload["content"]["pages"][self.records[0]["page_id"]]["status"] = "tampered"
        atomic_write_json(snapshot_path, payload)
        with self.assertRaises(BusinessError) as caught:
            SnapshotStore(corrupt_state).load(3)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.stage, "recognize")
        self.assertEqual(caught.exception.details["stage_number"], 3)
        self.assertIn("version", caught.exception.details)
        self.assertFalse(list(state.glob("*.tmp-*")))

        header_state = self.state("corrupt-header")
        shutil.copytree(state, header_state)
        header_path = header_state / "stage_3.json"
        header_payload = read_json(header_path)
        header_payload["version"] = int(header_payload["version"]) - 1
        atomic_write_json(header_path, header_payload)
        with self.assertRaises(BusinessError) as caught:
            SnapshotStore(header_state).load(3)
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")
        self.assertEqual(caught.exception.stage, "recognize")
        self.assertEqual(caught.exception.details["stage_number"], 3)

    def test_nonblocking_commit_guard_returns_stage_busy(self) -> None:
        state = self.state("busy")
        store = SnapshotStore(state)
        with CommitGuard(state):
            with self.assertRaises(BusinessError) as caught:
                store.commit_stage(
                    1,
                    stage_name="preprocess",
                    signature="busy",
                    content={
                        "pages": {
                            record["page_id"]: {"page_id": record["page_id"]}
                            for record in self.records
                        }
                    },
                    expected_pages={record["page_id"] for record in self.records},
                )
        self.assertEqual(caught.exception.code, E_STAGE_BUSY)

    def test_commit_guard_rejects_other_process_without_waiting(self) -> None:
        state = self.state("busy-process")
        state.mkdir(parents=True, exist_ok=True)
        context = multiprocessing.get_context("fork")
        ready = context.Event()
        release = context.Event()

        def hold_guard() -> None:
            with CommitGuard(state):
                ready.set()
                release.wait(5)

        process = context.Process(target=hold_guard)
        process.start()
        try:
            self.assertTrue(ready.wait(5))
            started = time.monotonic()
            with self.assertRaises(BusinessError) as caught:
                with CommitGuard(state):
                    pass
            elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.code, E_STAGE_BUSY)
            self.assertLess(elapsed, 0.5)
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join(2)

    def test_validation_codes_point_to_fields(self) -> None:
        config = PipelineConfig()
        duplicate = [dict(record) for record in self.records]
        duplicate[1] = dict(duplicate[1])
        duplicate[1]["page_id"] = duplicate[0]["page_id"]
        with self.assertRaises(BusinessError) as caught:
            validate_manifest(duplicate, config, self.library)
        self.assertEqual(caught.exception.code, E_DUPLICATE_PAGE_ID)
        self.assertEqual(caught.exception.field, "page_id")

        bad_shape = [dict(record) for record in self.records]
        bad_shape[0] = dict(bad_shape[0])
        bad_shape[0]["image_shape"] = [100, 100]
        with self.assertRaises(BusinessError) as caught:
            validate_manifest(bad_shape, config, self.library)
        self.assertEqual(caught.exception.code, E_IMAGE_SHAPE_MISMATCH)
        self.assertEqual(caught.exception.field, "image_shape")

        bad_charset = [dict(record) for record in self.records]
        bad_charset[0] = dict(bad_charset[0])
        bad_charset[0]["charset_version"] = "latin-v99"
        with self.assertRaises(BusinessError) as caught:
            validate_manifest(bad_charset, config, self.library)
        self.assertEqual(caught.exception.code, E_UNKNOWN_CHARSET_VERSION)
        self.assertEqual(caught.exception.field, "charset_version")

    def test_resource_limit_error_and_streaming_degrade(self) -> None:
        limited = PipelineConfig(
            version="resource-error-v1",
            max_candidate_comparisons=1,
            resource_policy="error",
        )
        error_result = PipelineRunner(
            self.data_dir,
            self.state("resource-error"),
            config=limited,
        ).run([], reset=True)
        self.assertIn("E_RESOURCE_LIMIT", error_result.report["business_codes"])
        self.assertTrue(
            any(
                "truth_text_resource_fallback" in {
                    item["category"] for item in page["degradations"]
                }
                for page in error_result.report["pages"]
            )
        )
        for page in error_result.report["pages"]:
            self.assertGreater(len(page["recognized_text"]), 0)

        streamed = PipelineConfig(
            version="resource-stream-v1",
            max_candidate_comparisons=1,
            resource_policy="stream",
        )
        stream_result = PipelineRunner(
            self.data_dir,
            self.state("resource-stream"),
            config=streamed,
        ).run([], reset=True)
        categories = {
            item["category"]
            for page in stream_result.report["pages"]
            for item in page["degradations"]
        }
        self.assertIn("streaming_line_recognition", categories)

        memory_limited = PipelineConfig(
            version="resource-memory-v1",
            max_feature_memory_bytes=1,
            resource_policy="error",
        )
        memory_result = PipelineRunner(
            self.data_dir,
            self.state("resource-memory"),
            config=memory_limited,
        ).run([], reset=True)
        self.assertIn("E_RESOURCE_LIMIT", memory_result.report["business_codes"])

        with self.assertRaises(BusinessError) as caught:
            validate_manifest(
                self.records,
                PipelineConfig(max_lines_per_page=1),
                self.library,
            )
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")
        self.assertEqual(caught.exception.field, "line_count")

    def test_unknown_charset_injection_uses_low_resolution_fallback(self) -> None:
        page = self.records[0]
        result = PipelineRunner(
            self.data_dir,
            self.state("charset-injection"),
        ).run(
            [
                {
                    "stage": "recognize",
                    "code": "E_UNKNOWN_CHARSET_VERSION",
                    "page_id": page["page_id"],
                    "params": {"charset_version": "latin-v99"},
                }
            ],
            reset=True,
        )
        self.assertIn("E_UNKNOWN_CHARSET_VERSION", result.report["business_codes"])
        degraded_page = next(
            item for item in result.report["pages"] if item["page_id"] == page["page_id"]
        )
        self.assertEqual(
            degraded_page["degradations"][0]["category"],
            "low_resolution_templates",
        )
        self.assertGreater(len(degraded_page["recognized_text"]), 0)

    def test_incomplete_stage_commit_is_rejected(self) -> None:
        store = SnapshotStore(self.state("incomplete"))
        expected = {record["page_id"] for record in self.records}
        with self.assertRaises(BusinessError) as caught:
            store.commit_stage(
                1,
                stage_name="preprocess",
                signature="incomplete",
                content={"pages": {self.records[0]["page_id"]: {"page_id": self.records[0]["page_id"]}}},
                expected_pages=expected,
            )
        self.assertEqual(caught.exception.code, "E_SNAPSHOT_CORRUPT")

    def test_reference_tolerance_failure_has_first_line_and_character(self) -> None:
        strict = PipelineConfig(
            version="strict-reference-v1",
            comparison_score_tolerance=0.0,
        )
        result = PipelineRunner(
            self.data_dir,
            self.state("tolerance"),
            config=strict,
        ).run([], reset=True)
        self.assertIn(E_TOLERANCE_EXCEEDED, result.report["business_codes"])
        find = [
            issue
            for page in result.report["pages"]
            for issue in page["issues"]
            if issue["code"] == E_TOLERANCE_EXCEEDED
        ]
        self.assertTrue(find)
        self.assertIsNotNone(find[0]["line_no"])
        first = find[0]["details"]["first_difference"]
        self.assertIn("char_position", first)
        self.assertIn("line_no", first)
        self.assertTrue(
            any(
                item["category"] == "high_precision_reference"
                for page in result.report["pages"]
                for item in page["degradations"]
            )
        )

    def test_template_upgrade_recomputes_only_affected_pages(self) -> None:
        state = self.state("upgrade")
        base = PipelineRunner(
            self.data_dir,
            state,
            library=get_template_library("glyph-v1"),
        ).run([], reset=True)
        old_versions = {
            stage: metadata["version"]
            for stage, metadata in base.snapshot_metadata["stages"].items()
        }
        store = SnapshotStore(state)
        expected = {record["page_id"] for record in self.records}
        stage3_before = store.load(3, expected_pages=expected)
        assert stage3_before is not None
        result = PipelineRunner(
            self.data_dir,
            state,
            library=get_template_library("glyph-v1"),
        ).upgrade_template_library(get_template_library("glyph-v2"))
        upgrade = result.runtime["upgrade"]
        self.assertEqual(upgrade["changed_characters"], ["7", "Q"])
        self.assertEqual(set(upgrade["recomputed_pages"]), set(upgrade["affected_pages"]))
        self.assertFalse(set(upgrade["recomputed_pages"]) & set(upgrade["reused_pages"]))
        self.assertTrue(
            set(upgrade["affected_pages"]).issubset(
                set(upgrade["recomputed_pages"]) | set(upgrade["reused_pages"])
            )
        )
        self.assertIn("page-0006", upgrade["reused_pages"])
        self.assertTrue(result.selfcheck["ok"])
        self.assertEqual(result.runtime["stage_versions"]["1"], old_versions["1"])
        self.assertEqual(result.runtime["stage_versions"]["2"], old_versions["2"])
        self.assertGreater(result.runtime["stage_versions"]["3"], old_versions["3"])
        self.assertGreater(result.runtime["stage_versions"]["4"], old_versions["4"])
        stage3_after = store.load(3, expected_pages=expected)
        assert stage3_after is not None
        reused_page = upgrade["reused_pages"][0]
        self.assertEqual(
            checksum(stage3_before.content["pages"][reused_page]),
            checksum(stage3_after.content["pages"][reused_page]),
        )
        with self.assertRaises(BusinessError) as caught:
            PipelineRunner(
                self.data_dir,
                state,
                library=get_template_library("glyph-v2"),
            ).upgrade_template_library(get_template_library("glyph-v1"))
        self.assertEqual(caught.exception.code, E_SNAPSHOT_ROLLBACK)

    def test_config_version_change_replaces_all_stage_snapshots(self) -> None:
        state = self.state("config-version")
        first = PipelineRunner(
            self.data_dir,
            state,
            config=PipelineConfig(version="rec-v1"),
        ).run([], reset=True)
        second = PipelineRunner(
            self.data_dir,
            state,
            config=PipelineConfig(version="rec-v2"),
        ).run([])
        self.assertNotEqual(first.report_bytes, second.report_bytes)
        self.assertEqual(second.report["config"]["version"], "rec-v2")
        self.assertEqual(set(second.runtime["recomputed_pages_by_stage"]), {"1", "2", "3", "4"})
        for stage in range(1, 5):
            self.assertGreater(
                second.snapshot_metadata["stages"][str(stage)]["version"],
                first.snapshot_metadata["stages"][str(stage)]["version"],
            )
            self.assertEqual(
                second.snapshot_metadata["stages"][str(stage)]["config_version"],
                "rec-v2",
            )

    def test_selfcheck_reports_inconsistent_stage_and_page(self) -> None:
        runner = PipelineRunner(self.data_dir, self.state("selfcheck-unit"))
        result = runner.run([], reset=True)
        store = SnapshotStore(self.state("selfcheck-unit"))
        expected = {record["page_id"] for record in self.records}
        contents = {
            stage: store.load(stage, expected_pages=expected).content["pages"]
            for stage in range(1, 5)
        }
        tampered = {stage: dict(pages) for stage, pages in contents.items()}
        tampered[3].pop(self.records[0]["page_id"])
        with self.assertRaises(BusinessError) as caught:
            runner._self_check(tampered, result.report["pages"], upgrade_context=None)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)
        self.assertIsNotNone(caught.exception.stage)
        self.assertIsNotNone(caught.exception.page_id)

    def test_unexpected_stage_failure_is_explicitly_unattributed(self) -> None:
        with patch(
            "ocrlab.pipeline.preprocess_page",
            side_effect=RuntimeError("synthetic unexpected failure"),
        ):
            with self.assertRaises(BusinessError) as caught:
                PipelineRunner(
                    self.data_dir,
                    self.state("unattributed"),
                ).run([], reset=True)
        self.assertEqual(caught.exception.code, E_INCONSISTENT)
        failures = caught.exception.details["failures"]
        self.assertTrue(
            any(
                "unattributed" in failure["message"]
                for failure in failures
            )
        )
        store = SnapshotStore(self.state("unattributed"))
        stage_one = store.load(1, expected_pages={record["page_id"] for record in self.records})
        assert stage_one is not None
        first_page = stage_one.content["pages"][self.records[0]["page_id"]]
        self.assertFalse(first_page["issues"][0]["attributed"])
        self.assertEqual(first_page["issues"][0]["code"], "E_UNATTRIBUTED")


class ServiceTests(unittest.TestCase):
    def test_edit_page_invalidates_snapshots_and_regenerates_image(self) -> None:
        from ocrlab.service import WorkspaceService

        root = Path(tempfile.mkdtemp(prefix="ocrlab-service-"))
        try:
            service = WorkspaceService(root / "data", root / "state", page_count=3)
            service.run([], reset=True)
            page = service.state()["pages"][0]
            lines = [
                {
                    "text": line["text"],
                    "truth_box": line["truth_box"],
                }
                for line in page["lines"]
            ]
            lines[0]["text"] = "ABC123"
            lines[0]["truth_box"] = [26, 22, 26 + len("ABC123") * 13, 38]
            edited = service.edit_page(page["page_id"], lines)
            self.assertEqual(edited["lines"][0]["text"], "ABC123")
            state = service.state()
            self.assertFalse(state["snapshots"]["stages"])
            self.assertEqual(state["report"], {})
            self.assertTrue((root / "data" / "images" / f"{page['page_id']}.png").exists())
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

