from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np

from qconv.config import load_schedule_config
from qconv.conv import integer_convolution
from qconv.engine import QConvEngine
from qconv.errors import AccOverflowError, PackShapeMismatchError
from qconv.graph import QuantNode, load_graph
from qconv.packs import read_pack
from qconv.sample import generate_samples
from qconv.util import dequantize_integer_array, atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class QConvIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="qconv-tests-")
        cls.root = Path(cls.temp.name)
        cls.samples = cls.root / "samples"
        generate_samples(cls.samples)
        cls.graph = cls.samples / "graph.json"
        cls.packs = cls.samples / "packs.json"
        cls.schedule_path = cls.samples / "schedule.json"
        cls.tight_schedule_path = cls.samples / "schedule-compact-budget.json"
        cls.schedule = load_schedule_config(cls.schedule_path)
        cls.tight_schedule = load_schedule_config(cls.tight_schedule_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def session(self, name: str) -> Path:
        return self.root / f"session-{name}-{self._testMethodName}"

    def new_engine(
        self,
        name: str,
        *,
        schedule=None,
        injection=None,
        graph: Path | None = None,
    ) -> QConvEngine:
        return QConvEngine(
            graph or self.graph,
            self.packs,
            schedule or self.schedule,
            self.session(name),
            injection,
        )

    def test_integer_convolution_and_overflow_position(self) -> None:
        node = QuantNode(
            id="n",
            input_tensor="x",
            output_tensor="n",
            in_channels=1,
            out_channels=1,
            kernel=(2, 2),
            stride=(1, 1),
            padding=(0, 0),
            weight_pack="w",
            weight_shape=(1, 1, 2, 2),
            weight_bit_width=8,
            weight_scale=1.0,
            activation_bit_width=8,
            activation_scale=1.0,
        )
        activation = np.asarray([[[[1, 2], [3, 4]]]], dtype=np.int64)
        weights = np.asarray([[[[1, 0], [0, 1]]]], dtype=np.int64)
        result, macs, overflows = integer_convolution(
            node, activation, weights, accumulator_bits=8, overflow_policy="error"
        )
        np.testing.assert_array_equal(result, np.asarray([[[[5]]]], dtype=np.int64))
        self.assertEqual(macs, 4)
        self.assertEqual(overflows, [])

        overflowing_weights = np.full((1, 1, 2, 2), 2, dtype=np.int64)
        with self.assertRaises(AccOverflowError) as raised:
            integer_convolution(
                node,
                np.full((1, 1, 2, 2), 2, dtype=np.int64),
                overflowing_weights,
                accumulator_bits=4,
                overflow_policy="error",
            )
        failure = raised.exception.to_failure()
        self.assertEqual(failure.code, "E_ACC_OVERFLOW")
        self.assertEqual(failure.node_id, "n")
        self.assertEqual(failure.details["position"]["kernel_col"], 1)
        self.assertEqual(failure.details["position"]["mac_index"], 1)

    def test_deterministic_dequant_rounding_modes(self) -> None:
        values = np.asarray([[1], [3]], dtype=np.int64)
        half_even, _ = dequantize_integer_array(values, 0.5, 1.0, "half_even", 0)
        half_up, _ = dequantize_integer_array(values, 0.5, 1.0, "half_up", 0)
        floor, _ = dequantize_integer_array(values, 0.5, 1.0, "floor", 0)
        ceil, _ = dequantize_integer_array(values, 0.5, 1.0, "ceil", 0)
        self.assertEqual(half_even.reshape(-1).tolist(), [0.0, 2.0])
        self.assertEqual(half_up.reshape(-1).tolist(), [1.0, 2.0])
        self.assertEqual(floor.reshape(-1).tolist(), [0.0, 1.0])
        self.assertEqual(ceil.reshape(-1).tolist(), [1.0, 2.0])

    def test_pack_shape_count_endian_and_truncation_are_strict(self) -> None:
        original = (self.samples / "packs" / "input.qpak").read_bytes()
        temp_pack = self.root / "bad-input.qpak"

        count_corrupt = bytearray(original)
        shape = (1, 4, 6, 6)
        count = int(np.prod(shape))
        struct.pack_into("<Q", count_corrupt, 12, count + 1)
        temp_pack.write_bytes(count_corrupt)
        with self.assertRaises(PackShapeMismatchError):
            read_pack(temp_pack)

        temp_pack.write_bytes(original[:-1])
        with self.assertRaises(PackShapeMismatchError):
            read_pack(temp_pack)

        endian_corrupt = bytearray(original)
        struct.pack_into("<H", endian_corrupt, 6, 0)
        temp_pack.write_bytes(endian_corrupt)
        with self.assertRaises(PackShapeMismatchError):
            read_pack(temp_pack)

        bad_samples = self.root / "bad-pack-samples"
        shutil.copytree(self.samples, bad_samples)
        (bad_samples / "packs" / "input.qpak").write_bytes(original[:-1])
        engine = QConvEngine(
            bad_samples / "graph.json",
            bad_samples / "packs.json",
            bad_samples / "schedule.json",
            self.session("bad-pack"),
        )
        report = engine.run()
        self.assertEqual(report["status"], "failed")
        self.assertIn("E_PACK_SHAPE_MISMATCH", [f["code"] for f in report["failures"]])

    def test_graph_validation_codes(self) -> None:
        base = json.loads(self.graph.read_text(encoding="utf-8"))

        duplicate = copy.deepcopy(base)
        duplicate["nodes"][1]["id"] = duplicate["nodes"][0]["id"]
        path = self.root / "graph-duplicate.json"
        path.write_text(json.dumps(duplicate), encoding="utf-8")
        result = load_graph(path)
        self.assertTrue(result.fatal)
        self.assertIn("E_GRAPH_INVALID", [failure.code for failure in result.failures])

        missing_edge = copy.deepcopy(base)
        missing_edge["edges"].append({"from": "does_not_exist", "to": "branch_l"})
        path = self.root / "graph-missing-edge.json"
        path.write_text(json.dumps(missing_edge), encoding="utf-8")
        result = load_graph(path)
        self.assertTrue(result.fatal)
        self.assertIn("E_GRAPH_INVALID", [failure.code for failure in result.failures])

        cycle = copy.deepcopy(base)
        cycle["edges"].append({"from": "tail", "to": "stem"})
        path = self.root / "graph-cycle.json"
        path.write_text(json.dumps(cycle), encoding="utf-8")
        result = load_graph(path)
        self.assertTrue(result.fatal)
        self.assertIn("E_GRAPH_INVALID", [failure.code for failure in result.failures])

        invalid_quant = copy.deepcopy(base)
        invalid_quant["nodes"][1]["weight_scale"] = float("nan")
        path = self.root / "graph-invalid-quant.json"
        path.write_text(json.dumps(invalid_quant), encoding="utf-8")
        result = load_graph(path)
        self.assertFalse(result.fatal)
        self.assertIn("E_QUANT_PARAM", [failure.code for failure in result.failures])
        engine = QConvEngine(
            path,
            self.packs,
            self.schedule,
            self.session("invalid-quant"),
        )
        report = engine.run()
        self.assertEqual(report["status"], "partial")
        self.assertIn("E_QUANT_PARAM", [failure["code"] for failure in report["failures"]])
        self.assertEqual(report["recomputed_nodes"], ["branch_l", "stem", "tail"])

    def test_normal_run_and_all_cache_reuse(self) -> None:
        session = self.session("normal")
        engine = QConvEngine(self.graph, self.packs, self.schedule, session)
        first = engine.run(session / "first.json")
        self.assertEqual(first["status"], "success")
        self.assertEqual(first["recomputed_nodes"], ["branch_l", "branch_r", "stem", "tail"])
        self.assertEqual(first["reused_nodes"], [])
        self.assertTrue((session / "first.json").exists())
        first_outputs = {node_id: result.output.copy() for node_id, result in engine.last_results.items()}

        second_engine = QConvEngine(self.graph, self.packs, self.schedule, session)
        second = second_engine.run(session / "second.json")
        self.assertEqual(second["status"], "success")
        self.assertEqual(second["recomputed_nodes"], [])
        self.assertEqual(second["reused_nodes"], ["branch_l", "branch_r", "stem", "tail"])
        self.assertEqual(second["restored_from_snapshot"], ["branch_l", "branch_r", "stem", "tail"])
        for node_id, expected in first_outputs.items():
            np.testing.assert_array_equal(second_engine.last_results[node_id].output, expected)
        for node in second["nodes"]:
            self.assertTrue(node["cache_hit"])
            self.assertEqual(node["macs_executed"], 0)
            self.assertIn("max_dequant_rounding_error", node["error_summary"])

    def normalized_matrix_result(self, engine: QConvEngine, report: dict) -> dict:
        return {
            "status": report["status"],
            "failures": [(failure["code"], failure.get("node_id")) for failure in report["failures"]],
            "recomputed": report["recomputed_nodes"],
            "reused": report["reused_nodes"],
            "ledger": report["budget"]["ledger"],
            "nodes": [
                (
                    node["node_id"],
                    node["status"],
                    node["integer_digest"],
                    node["output_digest"],
                    node["macs_executed"],
                )
                for node in report["nodes"]
            ],
        }

    def test_two_concurrency_levels_and_two_budgets_are_identical(self) -> None:
        for label, schedule in (("wide", self.schedule), ("tight", self.tight_schedule)):
            results = {}
            outputs = {}
            integers = {}
            for workers in (1, 4):
                engine = self.new_engine(
                    f"{label}-w{workers}",
                    schedule=replace(schedule, max_concurrency=workers),
                )
                report = engine.run(self.session(f"{label}-w{workers}") / "report.json")
                results[workers] = self.normalized_matrix_result(engine, report)
                outputs[workers] = {k: v.output.copy() for k, v in engine.last_results.items()}
                integers[workers] = {k: v.integer.copy() for k, v in engine.last_results.items()}
            self.assertEqual(results[1], results[4], label)
            self.assertEqual(sorted(outputs[1]), sorted(outputs[4]))
            for node_id in outputs[1]:
                np.testing.assert_array_equal(outputs[1][node_id], outputs[4][node_id])
                np.testing.assert_array_equal(integers[1][node_id], integers[4][node_id])
            if label == "tight":
                self.assertEqual(
                    results[1]["failures"],
                    [
                        ("E_BUDGET_EXHAUSTED", "stem"),
                        ("E_DEPENDENCY_FAILED", "tail"),
                    ],
                )

    def test_global_budget_exhaustion_is_reported(self) -> None:
        schedule = replace(
            self.schedule,
            global_mac_budget=5500,
            per_node_mac_quota=100000,
        )
        engine = self.new_engine("global-budget", schedule=schedule)
        report = engine.run()
        stem_row = next(row for row in report["budget"]["ledger"] if row["node_id"] == "stem")
        self.assertEqual(stem_row["status"], "budget_exhausted")
        self.assertEqual(stem_row["reason"], "global_mac_budget")
        self.assertEqual(stem_row["remaining_before"], 4096)
        self.assertIn(
            "E_BUDGET_EXHAUSTED",
            [(failure["code"]) for failure in report["failures"]],
        )

    def test_incremental_edits_and_clean_final_run_match(self) -> None:
        engine = self.new_engine("incremental")
        initial = engine.run()
        self.assertEqual(initial["status"], "success")
        original_branch_r = engine.last_results["branch_r"].output.copy()
        original_integer = engine.last_results["branch_r"].integer_digest
        original_stem = engine.last_results["stem"].output.copy()

        affected = engine.set_bit_width("branch_r", "activation", 4)
        self.assertEqual(affected, ["branch_r"])
        report_bits = engine.run()
        self.assertEqual(report_bits["invalidated_nodes"], ["branch_r"])
        self.assertEqual(report_bits["recomputed_nodes"], ["branch_r"])
        self.assertEqual(report_bits["reused_nodes"], ["branch_l", "stem", "tail"])
        self.assertNotEqual(engine.last_results["branch_r"].integer_digest, original_integer)
        self.assertFalse(np.array_equal(engine.last_results["branch_r"].output, original_branch_r))

        affected = engine.set_scale("stem", "weight", 0.01)
        self.assertEqual(affected, ["stem", "tail"])
        report_scale = engine.run()
        self.assertEqual(report_scale["invalidated_nodes"], ["stem", "tail"])
        self.assertEqual(report_scale["recomputed_nodes"], ["stem", "tail"])
        self.assertEqual(report_scale["reused_nodes"], ["branch_l", "branch_r"])
        self.assertFalse(np.array_equal(engine.last_results["stem"].output, original_stem))

        report_reuse = engine.run()
        self.assertEqual(report_reuse["recomputed_nodes"], [])
        self.assertEqual(report_reuse["reused_nodes"], ["branch_l", "branch_r", "stem", "tail"])
        edited_outputs = {k: v.output.copy() for k, v in engine.last_results.items()}
        edited_graph = self.root / "edited-graph.json"
        engine.save_graph(edited_graph)

        clean = QConvEngine(
            edited_graph,
            self.packs,
            self.schedule,
            self.session("incremental-clean"),
        )
        clean_report = clean.run()
        self.assertEqual(clean_report["status"], "success")
        self.assertEqual(sorted(edited_outputs), sorted(clean.last_results))
        for node_id in sorted(edited_outputs):
            np.testing.assert_array_equal(edited_outputs[node_id], clean.last_results[node_id].output)

        mode_before = engine.last_results["branch_l"].output.copy()
        engine.set_quantization_enabled(False)
        report_off = engine.run()
        self.assertFalse(report_off["quantization_enabled"])
        self.assertEqual(report_off["recomputed_nodes"], ["branch_l", "branch_r", "stem", "tail"])
        self.assertFalse(np.array_equal(mode_before, engine.last_results["branch_l"].output))

    def test_subgraph_replace_matches_clean_final_run(self) -> None:
        engine = self.new_engine("replace")
        engine.run()
        patch = {
            "nodes": [
                {
                    "id": "tail2",
                    "input": "stem",
                    "output": "tail2",
                    "in_channels": 4,
                    "out_channels": 2,
                    "kernel": [3, 3],
                    "stride": [1, 1],
                    "padding": [1, 1],
                    "weight_pack": "tail",
                    "weight_shape": [2, 4, 3, 3],
                    "weight_bit_width": 7,
                    "weight_scale": 0.03,
                    "activation_bit_width": 8,
                    "activation_scale": 0.03125,
                    "accumulator_bits": 32,
                    "overflow_policy": "error",
                }
            ],
            "edges": [{"from": "stem", "to": "tail2"}],
            "outputs": ["branch_l", "branch_r", "tail2"],
        }
        affected = engine.replace_subgraph("tail", patch)
        self.assertEqual(affected, ["tail2"])
        report = engine.run()
        self.assertEqual(report["status"], "success")
        self.assertEqual(report["recomputed_nodes"], ["tail2"])
        self.assertEqual(report["reused_nodes"], ["branch_l", "branch_r", "stem"])
        self.assertNotIn("tail", engine.last_results)
        self.assertIn("tail2", engine.last_results)
        incremental = {k: v.output.copy() for k, v in engine.last_results.items()}
        edited_graph = self.root / "replaced-graph.json"
        engine.save_graph(edited_graph)
        clean = QConvEngine(
            edited_graph,
            self.packs,
            self.schedule,
            self.session("replace-clean"),
        )
        clean.run()
        self.assertEqual(sorted(incremental), sorted(clean.last_results))
        for node_id in sorted(incremental):
            np.testing.assert_array_equal(incremental[node_id], clean.last_results[node_id].output)

    def test_injection_isolation_overflow_and_zero_cache(self) -> None:
        baseline = self.new_engine("injection-baseline")
        baseline.run()
        baseline_outputs = {k: v.output.copy() for k, v in baseline.last_results.items()}

        nan_injection = self.samples / "injection-nan.json"
        engine = self.new_engine("injection-nan", injection=nan_injection)
        report = engine.run()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(
            [(failure["code"], failure["node_id"]) for failure in report["failures"]],
            [("E_QUANT_PARAM", "branch_l")],
        )
        self.assertEqual([hit["id"] for hit in report["injection_hits"]], ["INJ-NAN-01"])
        self.assertEqual(report["reused_nodes"], [])
        for node_id in ("branch_r", "stem", "tail"):
            np.testing.assert_array_equal(engine.last_results[node_id].output, baseline_outputs[node_id])

        overflow_engine = self.new_engine(
            "injection-overflow",
            injection=self.samples / "injection-overflow.json",
        )
        overflow_report = overflow_engine.run()
        overflow_failures = [
            failure for failure in overflow_report["failures"] if failure["code"] == "E_ACC_OVERFLOW"
        ]
        self.assertEqual(len(overflow_failures), 1)
        self.assertEqual(overflow_failures[0]["node_id"], "branch_r")
        self.assertIn("position", overflow_failures[0]["details"])
        self.assertIn("INJ-OVF-01", [hit["id"] for hit in overflow_report["injection_hits"]])

        cache_engine = self.new_engine(
            "injection-cache-zero",
            injection=self.samples / "injection-cache-zero.json",
        )
        cache_report = cache_engine.run()
        self.assertEqual(cache_report["status"], "success")
        self.assertEqual(cache_report["cache"]["budget_bytes"], 0)
        self.assertEqual(cache_report["cache"]["artifact_count"], 0)
        self.assertEqual(cache_report["recomputed_nodes"], ["branch_l", "branch_r", "stem", "tail"])
        self.assertIn("INJ-CACHE-01", [hit["id"] for hit in cache_report["injection_hits"]])

    def test_injected_node_exception_stage_is_isolated(self) -> None:
        injection = {
            "schema_version": 1,
            "items": [
                {
                    "id": "INJ-EXC-01",
                    "type": "node_exception",
                    "node": "branch_l",
                    "value": "dequant",
                }
            ],
        }
        injection_path = self.root / "injection-node-exception.json"
        atomic_write_json(injection_path, injection)
        engine = self.new_engine("injection-exception", injection=injection_path)
        report = engine.run()
        failure = next(f for f in report["failures"] if f["code"] == "E_EXECUTION_FAILED")
        self.assertEqual(failure["node_id"], "branch_l")
        self.assertEqual(failure["stage"], "dequant")
        self.assertEqual(failure["exception_type"], "NodeExecutionError")
        self.assertEqual(report["recomputed_nodes"], ["branch_r", "stem", "tail"])

    def test_snapshot_config_mismatch(self) -> None:
        session = self.session("mismatch")
        QConvEngine(self.graph, self.packs, self.schedule, session).run()
        second = QConvEngine(self.graph, self.packs, self.tight_schedule, session)
        report = second.run()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failures"][0]["code"], "E_CONFIG_MISMATCH")
        self.assertEqual(second.last_results, {})

    @unittest.skipIf(os.name == "nt", "POSIX signal recovery is exercised on Linux")
    def test_graceful_signal_and_forced_kill_recovery(self) -> None:
        baseline_engine = self.new_engine("signal-baseline")
        baseline_engine.run()
        baseline = {k: v.output.copy() for k, v in baseline_engine.last_results.items()}

        graceful_schedule = replace(self.schedule, max_concurrency=1, debug_node_delay_ms=250)
        graceful_path = self.root / "schedule-graceful.json"
        atomic_write_json(graceful_path, graceful_schedule.to_dict())
        fast_path = self.root / "schedule-fast-resume.json"
        atomic_write_json(fast_path, replace(graceful_schedule, debug_node_delay_ms=0).to_dict())
        graceful_session = self.session("graceful")
        graceful_report_path = self.root / "graceful-report.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qconv",
                "run",
                "--graph",
                str(self.graph),
                "--packs",
                str(self.packs),
                "--schedule",
                str(graceful_path),
                "--session",
                str(graceful_session),
                "--report",
                str(graceful_report_path),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.55)
        process.send_signal(signal.SIGTERM)
        process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0)
        graceful_report = json.loads(graceful_report_path.read_text(encoding="utf-8"))
        self.assertTrue(graceful_report["interrupted"])
        self.assertEqual(graceful_report["status"], "interrupted")
        self.assertEqual(graceful_report["budget"]["remaining"], 93412)
        self.assertIn("skipped_graceful_stop", [node["status"] for node in graceful_report["nodes"]])

        resumed = QConvEngine(
            self.graph,
            self.packs,
            fast_path,
            graceful_session,
        )
        resume_report = resumed.run()
        self.assertEqual(resume_report["status"], "success")
        self.assertEqual(resume_report["restored_from_snapshot"], ["branch_l", "branch_r", "stem"])
        self.assertEqual(resume_report["recomputed_nodes"], ["tail"])
        for node_id, expected in baseline.items():
            np.testing.assert_array_equal(resumed.last_results[node_id].output, expected)

        kill_schedule = replace(self.schedule, max_concurrency=1, debug_node_delay_ms=400)
        kill_schedule_path = self.root / "schedule-kill.json"
        atomic_write_json(kill_schedule_path, kill_schedule.to_dict())
        kill_session = self.session("kill")
        kill_report_path = self.root / "kill-report.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qconv",
                "run",
                "--graph",
                str(self.graph),
                "--packs",
                str(self.packs),
                "--schedule",
                str(kill_schedule_path),
                "--session",
                str(kill_session),
                "--report",
                str(kill_report_path),
            ],
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        snapshot_path = kill_session / "snapshots" / "latest.json"
        completed: list[str] = []
        deadline = time.time() + 15
        while time.time() < deadline and process.poll() is None:
            if snapshot_path.exists():
                try:
                    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    snapshot = {}
                if len(snapshot.get("completed_nodes", [])) >= 3:
                    completed = snapshot["completed_nodes"]
                    break
            time.sleep(0.03)
        self.assertEqual(completed, ["branch_l", "branch_r", "stem"])
        process.kill()
        process.communicate(timeout=10)
        self.assertNotEqual(process.returncode, 0)
        self.assertFalse(kill_report_path.exists())
        completed_snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(completed_snapshot["completed_nodes"], ["branch_l", "branch_r", "stem"])

        kill_resume = QConvEngine(
            self.graph,
            self.packs,
            fast_path,
            kill_session,
        )
        kill_report = kill_resume.run()
        self.assertEqual(kill_report["status"], "success")
        self.assertEqual(kill_report["restored_from_snapshot"], ["branch_l", "branch_r", "stem"])
        self.assertEqual(kill_report["recomputed_nodes"], ["tail"])
        for node_id, expected in baseline.items():
            np.testing.assert_array_equal(kill_resume.last_results[node_id].output, expected)


if __name__ == "__main__":
    unittest.main()
