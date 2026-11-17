from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from layout_engine import LayoutEngine
from layout_engine.cli import main as cli_main
from layout_engine.columns import infer_columns
from layout_engine.config import ColumnConfig, LayoutConfig
from layout_engine.errors import ConfigError
from layout_engine.roles import classify_roles
from layout_engine.strategy import select_strategy
from tests.helpers import SAMPLES_DIR, canonical_pages, read_json, write_json


class AnalysisContractTests(unittest.TestCase):
    def test_sample_truth_for_both_reading_rules(self) -> None:
        base_config = LayoutConfig.from_path(SAMPLES_DIR / "config.json")
        for rule in ("column_first", "region_first"):
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as temporary:
                payload = copy.deepcopy(base_config.raw)
                payload["reading"]["rule"] = rule
                config = LayoutConfig.from_dict(payload)
                engine = LayoutEngine(
                    SAMPLES_DIR / "images",
                    config,
                    SAMPLES_DIR / "metadata.json",
                    Path(temporary) / "snapshot",
                    truth_path=SAMPLES_DIR / "layout_truth.json",
                )
                result = engine.run()
                self.assertEqual(result.exit_code, 0, result.report["failures"])
                validation = result.report["summary"]["truth_validation"]
                self.assertTrue(validation["all_passed"])
                self.assertEqual(
                    validation["pages_passed"],
                    result.report["summary"]["pages_processed"],
                )
                self.assertEqual(
                    result.report["summary"]["reading_order_entries"],
                    read_json(SAMPLES_DIR / "metadata.json")[
                        "expected_reading_order_entries"
                    ],
                )

    def test_repeated_full_runs_are_byte_stable_in_page_results(self) -> None:
        config = LayoutConfig.from_path(SAMPLES_DIR / "config.json")
        reports = []
        for run_index in range(2):
            with tempfile.TemporaryDirectory() as temporary:
                result = LayoutEngine(
                    SAMPLES_DIR / "images",
                    config,
                    SAMPLES_DIR / "metadata.json",
                    Path(temporary) / f"snapshot-{run_index}",
                    truth_path=SAMPLES_DIR / "layout_truth.json",
                ).run()
                reports.append(canonical_pages(result.report))
        self.assertEqual(reports[0], reports[1])

    def test_disabled_columns_are_a_real_single_group_degeneration(self) -> None:
        payload = read_json(SAMPLES_DIR / "config.json")
        payload["columns"]["enabled"] = False
        config = LayoutConfig.from_dict(payload)
        with tempfile.TemporaryDirectory() as temporary:
            result = LayoutEngine(
                SAMPLES_DIR / "images",
                config,
                SAMPLES_DIR / "metadata.json",
                Path(temporary) / "snapshot",
            ).run()
        self.assertEqual(result.exit_code, 0)
        for page in result.report["pages"]:
            self.assertFalse(page["diagnostics"]["column_inference_enabled"])
            self.assertEqual(page["diagnostics"]["column_mode"], "single_column_disabled")
            self.assertEqual(len(page["column_groups"]), 1)
            self.assertEqual(page["column_groups"][0]["column_id"], "c0")
            self.assertEqual(page["diagnostics"]["spanning_block_count"], 0)
            self.assertTrue(all(block["column_group"] == "c0" for block in page["blocks"]))
            self.assertEqual(
                page["column_groups"][0]["block_count"], page["block_count"]
            )

    def test_invalid_threshold_uses_config_invalid_code(self) -> None:
        payload = read_json(SAMPLES_DIR / "config.json")
        payload["analysis"]["binary_threshold"] = 255
        with self.assertRaises(ConfigError) as context:
            LayoutConfig.from_dict(payload)
        self.assertEqual(context.exception.code, "E_CONFIG_INVALID")

    def test_column_conflict_uses_stability_then_count(self) -> None:
        blocks = [
            {"block_id": "b001", "bbox": [0, 0, 100, 40], "ink_density": 0.2},
            {"block_id": "b002", "bbox": [120, 0, 80, 40], "ink_density": 0.2},
            {"block_id": "b003", "bbox": [0, 50, 100, 40], "ink_density": 0.2},
            {"block_id": "b004", "bbox": [120, 50, 120, 40], "ink_density": 0.2},
            {"block_id": "b005", "bbox": [40, 100, 130, 40], "ink_density": 0.2},
        ]
        roles = [
            {"block_id": "b001", "role": "body"},
            {"block_id": "b002", "role": "body"},
            {"block_id": "b003", "role": "body"},
            {"block_id": "b004", "role": "body"},
            {"block_id": "b005", "role": "title"},
        ]
        result = infer_columns(
            blocks,
            roles,
            config=ColumnConfig(
                enabled=True,
                gap_threshold=10,
                overlap_threshold=0.5,
                spanning_width_ratio=0.90,
            ),
        )
        self.assertEqual(result["assignments"]["b005"]["column_group"], "c0")
        conflict = result["assignments"]["b005"]["conflict"]
        self.assertIsNotNone(conflict)
        self.assertEqual(conflict["reason"], "multiple_column_overlap")
        self.assertIn("width_stability", conflict["decision"])

    def test_scale_selector_uses_auditable_fast_path(self) -> None:
        config = LayoutConfig.from_path(SAMPLES_DIR / "config.json")
        selected = select_strategy(
            page_count=5000,
            expected_entries=50_000,
            metadata={
                "layout_profile": {
                    "average_blocks_per_page": 10.0,
                    "average_column_groups": 2.0,
                }
            },
            config=config,
        )
        decision = selected["decision"]
        self.assertEqual(selected["effective"]["strategy_name"], "fast_resolution")
        self.assertEqual(selected["effective"]["detection_step"], 4)
        self.assertFalse(selected["effective"]["columns_enabled"])
        self.assertEqual(selected["effective"]["reading_rule"], "region_first")
        self.assertEqual(len(decision["candidates"]), 3)
        self.assertTrue(decision["selected"]["triggered_by"])
        self.assertGreater(decision["budget_impact"]["estimated_batches"], 1)

    def test_fast_strategy_really_changes_the_engine_path(self) -> None:
        payload = read_json(SAMPLES_DIR / "config.json")
        payload["runtime"]["batch_page_budget"] = 1
        config = LayoutConfig.from_dict(payload)
        with tempfile.TemporaryDirectory() as temporary:
            result = LayoutEngine(
                SAMPLES_DIR / "images",
                config,
                SAMPLES_DIR / "metadata.json",
                Path(temporary) / "snapshot",
            ).run()
        self.assertEqual(result.exit_code, 0, result.report["failures"])
        policy = result.report["effective_policy"]
        self.assertEqual(policy["strategy_name"], "fast_resolution")
        self.assertEqual(policy["detection_step"], 4)
        self.assertFalse(policy["columns_enabled"])
        self.assertEqual(policy["reading_rule"], "region_first")
        for page in result.report["pages"]:
            self.assertEqual(page["diagnostics"]["detection"]["resolution_step"], 4)
            self.assertFalse(page["diagnostics"]["column_inference_enabled"])
            self.assertEqual(page["diagnostics"]["reading_rule"], "region_first")

    def test_cli_config_error_enters_failure_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = read_json(SAMPLES_DIR / "config.json")
            payload["analysis"]["binary_threshold"] = 255
            config_path = root / "bad-config.json"
            report_path = root / "bad-report.json"
            write_json(config_path, payload)
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = cli_main(
                    [
                        "run",
                        "--images",
                        str(SAMPLES_DIR / "images"),
                        "--config",
                        str(config_path),
                        "--metadata",
                        str(SAMPLES_DIR / "metadata.json"),
                        "--snapshot",
                        str(root / "snapshot"),
                        "--report",
                        str(report_path),
                    ]
                )
            report = read_json(report_path)
        self.assertEqual(exit_code, 2)
        self.assertEqual(report["business_codes"], ["E_CONFIG_INVALID"])
        self.assertEqual(report["failures"][0]["code"], "E_CONFIG_INVALID")
        self.assertEqual(report["failures"][0]["stage"], "config")

    def test_boundary_equality_uses_configured_role_priority(self) -> None:
        payload = read_json(SAMPLES_DIR / "config.json")
        payload["analysis"]["body_height_ratio_range"] = [0.75, 1.15]
        payload["analysis"]["role_priority"] = [
            "body",
            "header_footer",
            "image",
            "title",
        ]
        config = LayoutConfig.from_dict(payload)
        blocks = [
            {
                "block_id": "b001",
                "bbox": [40, 210, 300, 100],
                "ink_density": 0.20,
                "horizontal_projection_periodicity": 0.50,
            },
            {
                "block_id": "b002",
                "bbox": [380, 210, 300, 100],
                "ink_density": 0.20,
                "horizontal_projection_periodicity": 0.50,
            },
            {
                "block_id": "b003",
                "bbox": [40, 330, 640, 115],
                "ink_density": 0.20,
                "horizontal_projection_periodicity": 0.50,
            },
        ]
        records, _ = classify_roles(blocks, page_height=960, config=config.analysis)
        target = records[2]
        self.assertEqual(target["role"], "body")
        self.assertEqual(
            target["role_evidence"]["decision"],
            "boundary_equality_role_priority",
        )
        self.assertIn("body", target["role_evidence"]["boundary_equal_roles"])
        self.assertIn("title", target["role_evidence"]["boundary_equal_roles"])


if __name__ == "__main__":
    unittest.main()
