from __future__ import annotations

import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path

from sparse_spmm.injection import run_injections
from sparse_spmm.reporting import report_digest_without_recovery
from sparse_spmm.runner import prepare_case, run_case
from sparse_spmm.utils import canonical_json_bytes
from tests.helpers import load_config


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temp = tempfile.TemporaryDirectory()
        cls.temp = Path(cls._temp.name)
        cls.case = prepare_case(load_config("sample_test.json"), load_config("strategies.json"))
        cls.serial = run_case(
            cls.case,
            report_path=cls.temp / "serial.json",
            progress_path=cls.temp / "serial_progress.json",
            max_workers=1,
            fresh=True,
        )
        cls.parallel = run_case(
            cls.case,
            report_path=cls.temp / "parallel.json",
            progress_path=cls.temp / "parallel_progress.json",
            max_workers=4,
            fresh=True,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temp.cleanup()

    def test_serial_and_parallel_reports_are_identical(self) -> None:
        self.assertEqual(self.serial.status, "COMPLETE")
        self.assertEqual(self.parallel.status, "COMPLETE")
        assert self.serial.report is not None and self.parallel.report is not None
        self.assertEqual(
            canonical_json_bytes(self.serial.report),
            canonical_json_bytes(self.parallel.report),
        )

    def test_order_variants_and_cross_references(self) -> None:
        assert self.serial.report is not None
        report = self.serial.report
        for order in ("asc", "desc", "b_abs_desc"):
            self.assertGreater(
                report["order_variants"][order]["delta_vs_reference_f32_summary"][
                    "nonzero_delta_count"
                ],
                0,
            )
        elements = report["attribution"]["elements"]
        layer_by_id = {layer["layer_id"]: layer for layer in report["layers"]["joint_layers"]}
        self.assertEqual(len(layer_by_id), len(report["layers"]["joint_layers"]))
        for element in elements:
            self.assertIn(element["layer_id"], layer_by_id)
            for contribution in element["top_contributions"]:
                self.assertIn(contribution["nz_id"], report["structure"]["nonzero_ids"])
        for layer in report["layers"]["joint_layers"]:
            for element_id in layer["violating_element_ids"]:
                matching = [element for element in elements if element["element_id"] == element_id]
                self.assertEqual(len(matching), 1)
                self.assertEqual(matching[0]["layer_id"], layer["layer_id"])

    def test_strict_tolerance_layers_have_nonempty_violation_cross_references(self) -> None:
        config = load_config("sample_test.json")
        config["execution"]["tolerance"]["relative"] = 5e-9
        case = prepare_case(config, load_config("strategies.json"))
        outcome = run_case(
            case,
            report_path=self.temp / "strict_tol.json",
            progress_path=self.temp / "strict_tol_progress.json",
            max_workers=2,
            fresh=True,
        )
        assert outcome.report is not None
        report = outcome.report
        self.assertGreater(report["statistics"]["tolerance_violation_count"], 0)
        elements = {element["element_id"]: element for element in report["attribution"]["elements"]}
        violating_from_elements = {
            element["element_id"] for element in elements.values() if element["exceeds_tolerance"]
        }
        violating_from_layers = {
            element_id
            for layer in report["layers"]["joint_layers"]
            for element_id in layer["violating_element_ids"]
        }
        self.assertEqual(violating_from_elements, violating_from_layers)
        for element_id in violating_from_elements:
            element = elements[element_id]
            layer = next(
                layer
                for layer in report["layers"]["joint_layers"]
                if layer["layer_id"] == element["layer_id"]
            )
            self.assertIn(element_id, layer["violating_element_ids"])
            self.assertGreater(len(element["top_contributions"]), 0)

    def test_injections_are_isolated_and_all_hit(self) -> None:
        B_before = self.case.B.copy()
        data_before = self.case.csr.data.copy()
        result = run_injections(self.case, load_config("injections.json"))
        self.assertEqual(result["attempted_count"], 5)
        self.assertEqual(result["hit_count"], 5)
        self.assertTrue(result["all_expected_codes_hit"])
        self.assertEqual(
            set(result["hit_ids"]),
            {
                "INJ-CSR-001",
                "INJ-NONFINITE-002",
                "INJ-SHAPE-003",
                "INJ-BLOCK-004",
                "INJ-TOL-005",
            },
        )
        self.assertEqual(
            {failure["business_code"] for failure in result["failures"]},
            {
                "E_CSR_INVALID",
                "E_NONFINITE_INPUT",
                "E_SHAPE_MISMATCH",
                "E_BLOCK_INVALID",
                "E_TOLERANCE_INVALID",
            },
        )
        for failure in result["failures"]:
            self.assertIn("shape", failure)
            self.assertIn("stage", failure)
            self.assertIn("exception_type", failure)
            self.assertIn("injection_id", failure)
        self.assertEqual(canonical_json_bytes(self.case.B.tolist()), canonical_json_bytes(B_before.tolist()))
        self.assertEqual(canonical_json_bytes(self.case.csr.data.tolist()), canonical_json_bytes(data_before.tolist()))

    def test_graceful_interrupt_and_resume_match_fresh_semantics(self) -> None:
        report_path = self.temp / "resume.json"
        progress_path = self.temp / "resume_progress.json"
        interrupted = run_case(
            self.case,
            report_path=report_path,
            progress_path=progress_path,
            max_workers=1,
            fresh=True,
            interrupt_after_blocks=1,
        )
        self.assertEqual(interrupted.status, "INTERRUPTED")
        self.assertEqual(interrupted.exit_code, 0)
        self.assertFalse(report_path.exists())
        self.assertEqual(interrupted.progress["status"], "INTERRUPTED")
        self.assertGreater(interrupted.progress["completed_row_count"], 0)
        resumed = run_case(
            self.case,
            report_path=report_path,
            progress_path=progress_path,
            max_workers=2,
            fresh=False,
        )
        self.assertEqual(resumed.status, "COMPLETE")
        assert resumed.report is not None and self.serial.report is not None
        self.assertEqual(resumed.report["recovery"]["mode"], "resume")
        self.assertGreater(resumed.report["recovery"]["resumed_block_count"], 0)
        self.assertEqual(
            report_digest_without_recovery(resumed.report),
            report_digest_without_recovery(self.serial.report),
        )

    def test_external_stop_event_finishes_with_exit_zero(self) -> None:
        event = threading.Event()
        event.set()
        outcome = run_case(
            self.case,
            report_path=self.temp / "stopped.json",
            progress_path=self.temp / "stopped_progress.json",
            max_workers=1,
            fresh=True,
            stop_event=event,
        )
        self.assertEqual(outcome.status, "INTERRUPTED")
        self.assertEqual(outcome.exit_code, 0)
        self.assertEqual(outcome.progress["status"], "INTERRUPTED")
        self.assertFalse((self.temp / "stopped.json").exists())

    def test_atomic_writer_leaves_no_temp_files(self) -> None:
        from sparse_spmm.utils import atomic_write_json

        target = self.temp / "atomic" / "payload.json"
        atomic_write_json(target, {"ok": True, "rows": [1, 2, 3]})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["ok"], True)
        leftovers = [path for path in target.parent.iterdir() if path.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
