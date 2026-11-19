from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from elastic_control import cli
from elastic_control.errors import BusinessError, ErrorCode
from elastic_control.models import load_input_bundle

from .support import SAMPLES, write_json


class ValidationTests(unittest.TestCase):
    def _raw_samples(self) -> tuple[dict, dict, dict]:
        return (
            json.loads((SAMPLES / "services.json").read_text()),
            json.loads((SAMPLES / "nodes.json").read_text()),
            json.loads((SAMPLES / "trace.json").read_text()),
        )

    def _assert_code(
        self,
        services: dict,
        nodes: dict,
        trace: dict,
        code: ErrorCode,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, payload in (
                ("services.json", services),
                ("nodes.json", nodes),
                ("trace.json", trace),
            ):
                write_json(root / name, payload)
            with self.assertRaises(BusinessError) as context:
                load_input_bundle(
                    root / "services.json",
                    root / "nodes.json",
                    root / "trace.json",
                )
            self.assertEqual(context.exception.code, code)

    def test_valid_samples(self) -> None:
        bundle = load_input_bundle(
            SAMPLES / "services.json",
            SAMPLES / "nodes.json",
            SAMPLES / "trace.json",
        )
        self.assertEqual(len(bundle.services), 2)
        self.assertEqual(len(bundle.node_pool.nodes), 3)
        self.assertEqual(len(bundle.trace.windows), 8)
        self.assertEqual(len(bundle.sample_digest), 64)

    def test_duplicate_service(self) -> None:
        services, nodes, trace = self._raw_samples()
        services["services"][1]["name"] = services["services"][0]["name"]
        self._assert_code(services, nodes, trace, ErrorCode.DUPLICATE_SERVICE)

    def test_duplicate_node(self) -> None:
        services, nodes, trace = self._raw_samples()
        nodes["nodes"][1]["name"] = nodes["nodes"][0]["name"]
        self._assert_code(services, nodes, trace, ErrorCode.DUPLICATE_NODE)

    def test_invalid_memory(self) -> None:
        services, nodes, trace = self._raw_samples()
        services["services"][0]["memory_mib_per_replica"] = 0
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_MEMORY)

    def test_invalid_compute(self) -> None:
        services, nodes, trace = self._raw_samples()
        services["services"][0]["compute_units_per_replica"] = -1
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_COMPUTE)

    def test_invalid_replica_bounds(self) -> None:
        services, nodes, trace = self._raw_samples()
        services["services"][0]["min_replicas"] = 7
        services["services"][0]["max_replicas"] = 6
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_REPLICA_BOUNDS)

    def test_invalid_cold_start(self) -> None:
        services, nodes, trace = self._raw_samples()
        services["services"][0]["cold_start_ticks"] = -1
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_COLD_START)

    def test_invalid_arrival_rate(self) -> None:
        services, nodes, trace = self._raw_samples()
        trace["windows"][0]["arrival_rate"] = -0.1
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_ARRIVAL_RATE)

    def test_invalid_sequence_length(self) -> None:
        services, nodes, trace = self._raw_samples()
        trace["windows"][0]["avg_sequence_length"] = -1
        self._assert_code(services, nodes, trace, ErrorCode.INVALID_SEQUENCE_LENGTH)

    def test_non_finite_json_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            services, nodes, trace = self._raw_samples()
            trace["windows"][0]["arrival_rate"] = float("nan")
            (root / "trace.json").write_text(
                json.dumps(trace).replace("NaN", "NaN"),
                encoding="utf-8",
            )
            write_json(root / "services.json", services)
            write_json(root / "nodes.json", nodes)
            with self.assertRaises(BusinessError) as context:
                load_input_bundle(
                    root / "services.json",
                    root / "nodes.json",
                    root / "trace.json",
                )
            self.assertEqual(context.exception.code, ErrorCode.INPUT_JSON)

    def test_invalid_cli_does_not_create_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            services, nodes, trace = self._raw_samples()
            services["services"][0]["memory_mib_per_replica"] = -5
            write_json(root / "services.json", services)
            write_json(root / "nodes.json", nodes)
            write_json(root / "trace.json", trace)
            db_path = root / "state.db"
            runtime_dir = root / "runtime"
            stdout = io.StringIO()
            stderr = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = cli.main(
                    [
                        "run-round",
                        "--services",
                        str(root / "services.json"),
                        "--nodes",
                        str(root / "nodes.json"),
                        "--trace",
                        str(root / "trace.json"),
                        "--db",
                        str(db_path),
                        "--runtime-dir",
                        str(runtime_dir),
                        "--round-id",
                        "invalid",
                        "--tick",
                        "0",
                    ]
                )
            self.assertEqual(code, 2)
            self.assertFalse(db_path.exists())
            self.assertFalse(runtime_dir.exists())
            error = json.loads(stderr.getvalue())
            self.assertEqual(error["error"]["code"], ErrorCode.INVALID_MEMORY)

    def test_missing_window_and_unknown_service_do_not_create_state(self) -> None:
        for extra, expected in (
            (["--tick", "999"], ErrorCode.TRACE_WINDOW_MISSING),
            (["--tick", "0", "--service", "missing"], ErrorCode.UNKNOWN_SERVICE),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                db_path = root / "state.db"
                runtime_dir = root / "runtime"
                stdout = io.StringIO()
                stderr = io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(
                        [
                            "run-round",
                            "--services",
                            str(SAMPLES / "services.json"),
                            "--nodes",
                            str(SAMPLES / "nodes.json"),
                            "--trace",
                            str(SAMPLES / "trace.json"),
                            "--db",
                            str(db_path),
                            "--runtime-dir",
                            str(runtime_dir),
                            "--round-id",
                            "preflight",
                            *extra,
                        ]
                    )
                self.assertEqual(code, 2)
                self.assertFalse(db_path.exists())
                self.assertFalse(runtime_dir.exists())
                error = json.loads(stderr.getvalue())
                self.assertEqual(error["error"]["code"], expected)


if __name__ == "__main__":
    unittest.main()

