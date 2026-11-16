from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from convedit.engine import Engine
from convedit.errors import BusinessError, E_CONFIG_MISMATCH, E_DEPENDENCY_FAILED, E_PARAM_OUT_OF_RANGE
from convedit.graph import load_graph_file, validate_graph
from convedit.injections import load_injections
from convedit.inputs import generate_input, load_input_descriptor
from convedit.ops import bilinear, conv2d, nearest
from convedit.runtime import StopController
from convedit.state import StateStore
from convedit.util import load_json


ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def _sample_raw() -> tuple[dict, dict, dict, dict]:
    return (
        load_json(SAMPLES / "graph.json"),
        load_json(SAMPLES / "input.json"),
        load_json(SAMPLES / "config.json"),
        load_json(SAMPLES / "edits.json"),
    )


def _make_state(
    root: Path,
    *,
    graph_raw: dict | None = None,
    config_raw: dict | None = None,
    graph_version: int = 0,
) -> tuple[StateStore, np.ndarray]:
    graph_raw = copy.deepcopy(graph_raw if graph_raw is not None else _sample_raw()[0])
    descriptor_raw = copy.deepcopy(_sample_raw()[1])
    config_raw = copy.deepcopy(config_raw if config_raw is not None else _sample_raw()[2])
    validation = validate_graph(graph_raw)
    if validation.graph is None:
        raise AssertionError(validation.failures)
    generated = generate_input(load_input_descriptor(SAMPLES / "input.json"), validation.graph)
    store = StateStore(root)
    store.initialize(
        graph_raw=graph_raw,
        config_raw=config_raw,
        input_descriptor_raw=descriptor_raw,
        input_array=generated.tensor,
        graph_version=graph_version,
    )
    return store, generated.tensor


def _fresh_run(root: Path, graph_raw: dict, config_raw: dict, version: int, input_array: np.ndarray):
    descriptor_raw = copy.deepcopy(_sample_raw()[1])
    store = StateStore(root)
    store.initialize(
        graph_raw=copy.deepcopy(graph_raw),
        config_raw=copy.deepcopy(config_raw),
        input_descriptor_raw=descriptor_raw,
        input_array=input_array,
        graph_version=version,
    )
    return store, Engine(store, config_raw).run(store.load())


def _assert_products_equal(testcase: unittest.TestCase, left: StateStore, right: StateStore) -> None:
    left_snapshot = left.load()
    right_snapshot = right.load()
    graph = validate_graph(left_snapshot["graph"]).graph
    testcase.assertIsNotNone(graph)
    for node_id in graph.topo_order:
        left_record = left_snapshot["node_products"].get(node_id)
        right_record = right_snapshot["node_products"].get(node_id)
        testcase.assertIsNotNone(left_record, node_id)
        testcase.assertIsNotNone(right_record, node_id)
        testcase.assertEqual(left_record["tensor_fingerprint"], right_record["tensor_fingerprint"], node_id)
        testcase.assertEqual(left_record["cache_key"], right_record["cache_key"], node_id)
        left_array = left.load_product(left_record)
        right_array = right.load_product(right_record)
        testcase.assertIsNotNone(left_array)
        testcase.assertIsNotNone(right_array)
        testcase.assertTrue(np.array_equal(left_array, right_array, equal_nan=True), node_id)
    testcase.assertEqual(left_snapshot["final_outputs"], right_snapshot["final_outputs"])
    testcase.assertEqual(left_snapshot["failures"], right_snapshot["failures"])


class GraphValidationTests(unittest.TestCase):
    def test_topo_and_fingerprint_are_stable(self) -> None:
        validation = load_graph_file(SAMPLES / "graph.json")
        self.assertTrue(validation.ok)
        self.assertEqual(
            validation.graph.topo_order,
            ["conv1", "conv3", "interp_bi", "conv2", "interp_nn"],
        )
        second = load_graph_file(SAMPLES / "graph.json")
        self.assertEqual(validation.graph.fingerprint, second.graph.fingerprint)
        self.assertEqual(
            validation.graph.topology_records()[0]["stable_sort_key"],
            "00000000:conv1",
        )

    def test_duplicate_node_and_multiple_producer(self) -> None:
        raw, _, _, _ = _sample_raw()
        duplicate = copy.deepcopy(raw)
        duplicate["nodes"].append(copy.deepcopy(duplicate["nodes"][0]))
        result = validate_graph(duplicate)
        self.assertFalse(result.ok)
        self.assertIn("E_GRAPH_INVALID", {item["code"] for item in result.failures})

        multiple = copy.deepcopy(raw)
        for node in multiple["nodes"]:
            if node["id"] == "conv3":
                node["tensor"] = "conv1"
        result = validate_graph(multiple)
        self.assertFalse(result.ok)
        self.assertTrue(any("more than one producer" in item["message"] for item in result.failures))

    def test_missing_edge_endpoint_and_cycle(self) -> None:
        raw, _, _, _ = _sample_raw()
        missing = copy.deepcopy(raw)
        missing["edges"][0]["to"] = "not-a-node"
        result = validate_graph(missing)
        self.assertFalse(result.ok)
        self.assertIn("E_GRAPH_INVALID", {item["code"] for item in result.failures})

        cyclic = copy.deepcopy(raw)
        cyclic["edges"] = [
            edge for edge in cyclic["edges"] if edge["id"] != "e_input_conv1"
        ]
        cyclic["edges"].append(
            {"id": "e_cycle", "from": "conv2", "to": "conv1", "tensor": "conv2"}
        )
        result = validate_graph(cyclic)
        self.assertFalse(result.ok)
        self.assertTrue(any("cycle" in item["message"] for item in result.failures))

    def test_parameter_business_code(self) -> None:
        raw, _, _, _ = _sample_raw()
        for node in raw["nodes"]:
            if node["id"] == "conv1":
                node["params"]["kernel_size"] = [0, 3]
        result = validate_graph(raw)
        self.assertFalse(result.parameter_valid)
        self.assertIn(E_PARAM_OUT_OF_RANGE, {item["code"] for item in result.failures})
        self.assertEqual(result.failures[0]["node_id"], "conv1")


class OperatorTests(unittest.TestCase):
    def test_conv2d_unit_kernel_and_fixed_order_metadata(self) -> None:
        x = np.arange(9, dtype=np.float64).reshape(1, 1, 3, 3)
        params = {
            "kernel_size": [1, 1],
            "stride": [1, 1],
            "padding": [0, 0],
            "in_channels": 1,
            "out_channels": 1,
            "weights": [[[[2.0]]]],
            "bias": [1.5],
        }
        result = conv2d(x, params, padding_value=0.0)
        self.assertEqual(result.tensor.dtype, np.float64)
        self.assertTrue(np.array_equal(result.tensor, x * 2.0 + 1.5))
        self.assertEqual(result.metadata["loop_order"][-3:], ["input_channel", "kernel_row", "kernel_col"])
        self.assertEqual(result.metadata["padding_value"], 0.0)

    def test_conv2d_nonzero_padding_value(self) -> None:
        x = np.array([[[[1.0]]]], dtype=np.float64)
        params = {
            "kernel_size": [3, 3],
            "stride": [1, 1],
            "padding": [1, 1],
            "in_channels": 1,
            "out_channels": 1,
            "weights": [[[[1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]]],
            "bias": [0.0],
        }
        result = conv2d(x, params, padding_value=2.0)
        self.assertEqual(result.tensor.shape, (1, 1, 1, 1))
        self.assertEqual(float(result.tensor[0, 0, 0, 0]), 17.0)

    def test_bilinear_and_nearest_metadata(self) -> None:
        x = np.array([[[[0.0, 1.0], [2.0, 3.0]]]], dtype=np.float64)
        bilinear_result = bilinear(
            x, {"output_size": [3, 3], "align_mode": "half_pixel"}
        )
        self.assertEqual(bilinear_result.tensor.shape, (1, 1, 3, 3))
        self.assertEqual(bilinear_result.metadata["alignment"], "half_pixel")
        self.assertEqual(bilinear_result.metadata["rounding_rule"], "floor_ceil")
        self.assertEqual(bilinear_result.metadata["boundary"], "clip")
        nearest_result = nearest(
            x, {"output_size": [4, 4], "align_mode": "asymmetric"}
        )
        self.assertEqual(nearest_result.metadata["alignment"], "asymmetric")
        self.assertEqual(nearest_result.metadata["rounding_rule"], "floor")
        self.assertTrue(np.array_equal(nearest_result.tensor[0, 0, :, 0], [0.0, 0.0, 2.0, 2.0]))


class IncrementalTests(unittest.TestCase):
    def test_alignment_edit_invalidates_only_chain_and_matches_full(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, input_array = _make_state(root / "incremental")
            config = _sample_raw()[2]
            initial = Engine(store, config).run(store.load())
            self.assertEqual(initial.report["status"], "complete")
            edit = {
                "id": "ALIGN",
                "op": "update_params",
                "node_id": "interp_bi",
                "params": {"align_mode": "asymmetric"},
            }
            engine = Engine(store, config)
            commit = engine.commit_edit(initial.snapshot, edit)
            self.assertTrue(commit.committed)
            self.assertEqual(commit.invalidated_nodes, ["interp_bi", "conv2"])
            self.assertEqual(commit.reused_nodes, ["conv1", "conv3", "interp_nn"])
            outcome = engine.run(commit.snapshot)
            self.assertEqual(outcome.report["recalculated_nodes"], ["interp_bi", "conv2"])
            self.assertEqual(outcome.report["reused_nodes"], ["conv1", "conv3", "interp_nn"])
            self.assertTrue(
                set(outcome.report["execution_trace"]).isdisjoint(outcome.report["reused_nodes"])
            )
            snapshot = store.load()
            interp_record = snapshot["node_products"]["interp_bi"]
            self.assertEqual(interp_record["operator_metadata"]["alignment"], "asymmetric")
            self.assertEqual(interp_record["operator_metadata"]["rounding_rule"], "floor_ceil")
            baseline_store, _ = _fresh_run(
                root / "baseline",
                snapshot["graph"],
                config,
                snapshot["graph_version"],
                input_array,
            )
            _assert_products_equal(self, store, baseline_store)

    def test_disable_interpolation_blocks_downstream_and_invalidates_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _ = _make_state(Path(directory) / "state")
            config = _sample_raw()[2]
            engine = Engine(store, config)
            initial = engine.run(store.load())
            commit = engine.commit_edit(
                initial.snapshot,
                {"id": "DISABLE", "op": "set_enabled", "node_id": "interp_bi", "enabled": False},
            )
            self.assertTrue(commit.committed)
            self.assertEqual(commit.invalidated_nodes, ["interp_bi", "conv2"])
            outcome = engine.run(commit.snapshot)
            statuses = {node["node_id"]: node["status"] for node in outcome.report["nodes"]}
            self.assertEqual(statuses["interp_bi"], "skipped_disabled")
            self.assertEqual(statuses["conv2"], "skipped_dependency")
            self.assertEqual(statuses["conv3"], "reused")
            self.assertEqual(statuses["interp_nn"], "reused")
            self.assertIn(E_DEPENDENCY_FAILED, outcome.report["business_codes"])

    def test_full_edit_sequence_matches_each_fresh_full_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, input_array = _make_state(root / "incremental")
            config = _sample_raw()[2]
            edits = _sample_raw()[3]["operations"]
            engine = Engine(store, config)
            outcome = engine.run(store.load())
            for index, operation in enumerate(edits, start=1):
                commit = engine.commit_edit(outcome.snapshot, operation)
                self.assertTrue(commit.committed, commit.failures)
                outcome = engine.run(commit.snapshot)
                self.assertEqual(outcome.report["status"], "complete")
                snapshot = store.load()
                baseline_store, _ = _fresh_run(
                    root / f"baseline_{index}",
                    snapshot["graph"],
                    config,
                    snapshot["graph_version"],
                    input_array,
                )
                _assert_products_equal(self, store, baseline_store)


class RecoveryTests(unittest.TestCase):
    def test_graceful_signal_snapshot_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, input_array = _make_state(Path(directory) / "state")
            config = _sample_raw()[2]
            injections = load_injections(
                SAMPLES / "injections.json", enable_ids=["INJ-INT-GRACEFUL"]
            )
            controller = StopController()
            engine = Engine(store, config, injections)
            with controller.installed_handlers():
                interrupted = engine.run(store.load(), stop_controller=controller)
            self.assertEqual(interrupted.report["status"], "interrupted")
            self.assertEqual(interrupted.report["exit_code"], 0)
            self.assertEqual(len(interrupted.report["completed_nodes"]), 4)
            self.assertEqual(
                interrupted.report["nodes"][3]["status"],
                "pending",
            )
            self.assertTrue(
                any(hit["id"] == "INJ-INT-GRACEFUL" for hit in interrupted.report["injections_hit"])
            )
            resumed = Engine(store, config).run(store.load())
            self.assertEqual(resumed.report["status"], "complete")
            self.assertEqual(resumed.report["reused_nodes"], ["conv1", "conv3", "interp_bi", "interp_nn"])
            self.assertEqual(resumed.report["recalculated_nodes"], ["conv2"])
            baseline_store, _ = _fresh_run(
                Path(directory) / "baseline",
                store.load()["graph"],
                config,
                0,
                input_array,
            )
            _assert_products_equal(self, store, baseline_store)

    def test_cli_sigterm_process_exits_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, _ = _make_state(root / "state")
            report_path = root / "interrupted.json"
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "convedit",
                    "run",
                    "--state",
                    str(store.root),
                    "--report",
                    str(report_path),
                    "--injections",
                    str(SAMPLES / "injections.json"),
                    "--enable-injection",
                    "INJ-INT-GRACEFUL",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(report["exit_code"], 0)
            self.assertEqual(len(report["completed_nodes"]), 4)
            self.assertTrue(
                any(hit["id"] == "INJ-INT-GRACEFUL" for hit in report["injections_hit"])
            )

    def test_config_and_graph_mismatch_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _ = _make_state(Path(directory) / "state")
            snapshot = store.load()
            with self.assertRaises(BusinessError) as context:
                store.verify_resume(snapshot, config_raw={**store.load_config(), "max_workers": 2})
            self.assertEqual(context.exception.code, E_CONFIG_MISMATCH)
            changed_graph = copy.deepcopy(snapshot["graph"])
            changed_graph["graph_id"] = "changed"
            with self.assertRaises(BusinessError) as context:
                store.verify_resume(snapshot, graph_raw=changed_graph)
            self.assertEqual(context.exception.code, E_CONFIG_MISMATCH)

    def test_real_sigkill_then_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store, input_array = _make_state(root / "state")
            config = _sample_raw()[2]
            hard_report = root / "hard_run_should_not_exist.json"
            command = [
                sys.executable,
                "-m",
                "convedit",
                "run",
                "--state",
                str(store.root),
                "--report",
                str(hard_report),
                "--injections",
                str(SAMPLES / "injections.json"),
                "--enable-injection",
                "INJ-KILL-HARD",
            ]
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(process.returncode, -9, process.stderr)
            self.assertFalse(hard_report.exists())
            killed_snapshot = store.load()
            self.assertEqual(len(killed_snapshot["completed_nodes"]), 4)
            self.assertTrue(
                any(hit["id"] == "INJ-KILL-HARD" for hit in killed_snapshot["injections_hit"])
            )
            resume_report = root / "resumed.json"
            resume = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "convedit",
                    "resume",
                    "--state",
                    str(store.root),
                    "--report",
                    str(resume_report),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(resume.returncode, 0, resume.stderr)
            resumed = json.loads(resume_report.read_text(encoding="utf-8"))
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["reused_nodes"], ["conv1", "conv3", "interp_bi", "interp_nn"])
            self.assertEqual(resumed["recalculated_nodes"], ["conv2"])
            baseline_store, _ = _fresh_run(
                root / "baseline", store.load()["graph"], config, 0, input_array
            )
            _assert_products_equal(self, store, baseline_store)


class FaultAndConcurrencyTests(unittest.TestCase):
    def test_shape_fault_is_isolated_and_hit_id_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _ = _make_state(Path(directory) / "state")
            config = _sample_raw()[2]
            injections = load_injections(
                SAMPLES / "injections.json", enable_ids=["INJ-SHAPE-001"]
            )
            outcome = Engine(store, config, injections).run(store.load())
            statuses = {node["node_id"]: node["status"] for node in outcome.report["nodes"]}
            self.assertEqual(statuses["conv1"], "failed")
            self.assertEqual(statuses["conv3"], "recalculated")
            self.assertEqual(statuses["interp_nn"], "recalculated")
            self.assertEqual(statuses["interp_bi"], "skipped_dependency")
            self.assertEqual(statuses["conv2"], "skipped_dependency")
            self.assertIn("E_SHAPE_MISMATCH", outcome.report["business_codes"])
            self.assertTrue(
                any(hit["id"] == "INJ-SHAPE-001" for hit in outcome.report["injections_hit"])
            )
            failures = outcome.report["failures"]
            self.assertEqual(
                [failure["node_id"] for failure in failures],
                ["conv1", "interp_bi", "conv2"],
            )

    def test_invalid_scale_and_nonfinite_input_codes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = _sample_raw()[2]
            scale_store, _ = _make_state(root / "scale")
            scale_injections = load_injections(
                SAMPLES / "injections.json", enable_ids=["INJ-SCALE-001"]
            )
            scale_outcome = Engine(scale_store, config, scale_injections).run(scale_store.load())
            self.assertIn(E_PARAM_OUT_OF_RANGE, scale_outcome.report["business_codes"])
            scale_status = {node["node_id"]: node["status"] for node in scale_outcome.report["nodes"]}
            self.assertEqual(scale_status["interp_bi"], "failed")
            self.assertEqual(scale_status["conv2"], "skipped_dependency")
            self.assertEqual(scale_status["interp_nn"], "recalculated")

            nonfinite_store, _ = _make_state(root / "nonfinite")
            nonfinite_injections = load_injections(
                SAMPLES / "injections.json", enable_ids=["INJ-NONFINITE-001"]
            )
            nonfinite_outcome = Engine(
                nonfinite_store, config, nonfinite_injections
            ).run(nonfinite_store.load())
            self.assertIn("E_NONFINITE_INPUT", nonfinite_outcome.report["business_codes"])
            self.assertTrue(
                any(
                    hit["id"] == "INJ-NONFINITE-001"
                    for hit in nonfinite_outcome.report["injections_hit"]
                )
            )

    def test_serial_and_parallel_products_and_lists_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            graph_raw, _, config, _ = _sample_raw()
            serial_config = copy.deepcopy(config)
            serial_config["max_workers"] = 1
            parallel_config = copy.deepcopy(config)
            parallel_config["max_workers"] = 4
            serial_store, input_array = _make_state(
                root / "serial", config_raw=serial_config
            )
            parallel_store, _ = _make_state(root / "parallel", config_raw=parallel_config)
            serial_outcome = Engine(serial_store, serial_config).run(serial_store.load())
            parallel_outcome = Engine(parallel_store, parallel_config).run(parallel_store.load())
            self.assertEqual(
                serial_outcome.report["recalculated_nodes"],
                parallel_outcome.report["recalculated_nodes"],
            )
            self.assertEqual(
                serial_outcome.report["reused_nodes"],
                parallel_outcome.report["reused_nodes"],
            )
            self.assertEqual(serial_outcome.report["failures"], parallel_outcome.report["failures"])
            self.assertEqual(
                [node["node_id"] for node in serial_outcome.report["nodes"]],
                [node["node_id"] for node in parallel_outcome.report["nodes"]],
            )
            _assert_products_equal(self, serial_store, parallel_store)

    def test_repeat_is_reuse_only_and_phase_snapshots_exist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, _ = _make_state(Path(directory) / "state")
            config = _sample_raw()[2]
            engine = Engine(store, config)
            first = engine.run(store.load())
            self.assertEqual(first.report["status"], "complete")
            second = engine.run(store.load())
            self.assertEqual(second.report["recalculated_nodes"], [])
            self.assertEqual(len(second.report["reused_nodes"]), 5)
            self.assertEqual(second.report["execution_trace"], [])
            archive_names = sorted(
                path.name for path in (store.root / "snapshots" / "v000000").glob("*.json")
            )
            joined = "\n".join(archive_names)
            self.assertIn("topology_scheduled", joined)
            self.assertIn("node_execution", joined)
            self.assertIn("product_summary", joined)
            self.assertIn("complete", joined)
            self.assertTrue((store.root / "current.json").is_file())


if __name__ == "__main__":
    unittest.main()
