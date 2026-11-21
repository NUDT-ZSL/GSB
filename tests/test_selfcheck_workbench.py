from __future__ import annotations

import copy
import json
import threading
import unittest
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory

from dimdiag.config import validate_config
from dimdiag.data import load_dataset
from dimdiag.engine import DiagnosticEngine, run_self_check
from dimdiag.errors import E_INCONSISTENT, BusinessError
from dimdiag.serialization import pretty_json_bytes, sha256_json
from dimdiag.snapshot import finalize_snapshot, verify_snapshot
from dimdiag.workbench import make_server

from .helpers import base_config, prepare_workspace


class SelfcheckWorkbenchTests(unittest.TestCase):
    def test_selfcheck_passes_for_random_projection_and_two_stage(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            for method, category in (
                ("random_projection", "random_projection_matrix_reproducible"),
                ("pca_random_projection", "random_projection_and_two_stage_consistency"),
            ):
                config = base_config(config_path)
                config["method"] = method
                outcome = engine.run(config, base_dir=config_path.parent)
                categories = {
                    check["category"]: check
                    for check in outcome.selfcheck["checks"]
                }
                self.assertTrue(outcome.selfcheck["overall_passed"])
                self.assertIn(category, categories)
                self.assertTrue(categories[category]["passed"])

    def test_selfcheck_detects_evaluation_version_mismatch(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            outcome = engine.run(config, base_dir=config_path.parent)
            snapshot = copy.deepcopy(outcome.snapshot)
            evaluation = snapshot["sections"]["evaluation"]
            evaluation["content"]["coordinates_version"] -= 1
            evaluation["checksum"] = sha256_json(evaluation["content"])
            snapshot = finalize_snapshot(snapshot)
            verify_snapshot(snapshot)
            dataset = load_dataset(config, config_path.parent)
            with self.assertRaises(BusinessError) as raised:
                run_self_check(
                    snapshot,
                    dataset,
                    config,
                    raise_on_fail=True,
                )
            self.assertEqual(raised.exception.code, E_INCONSISTENT)
            self.assertIn(
                "evaluation_coordinate_version",
                raised.exception.details["categories"],
            )

    def test_workbench_http_run_and_selfcheck(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            server = make_server(config_path, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            host, port = server.server_address[:2]
            base_url = f"http://{host}:{port}"
            try:
                with urllib.request.urlopen(f"{base_url}/") as response:
                    html = response.read().decode("utf-8")
                self.assertIn("DimDiag", html)
                with urllib.request.urlopen(f"{base_url}/api/state") as response:
                    state = json.loads(response.read())
                self.assertTrue(state["ok"])
                self.assertIsNone(state["active"])

                body = json.dumps(
                    {
                        "method": "random_projection",
                        "precision_mode": "float64",
                        "threshold_strategy": "tolerance_band",
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"{base_url}/api/run",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    run_result = json.loads(response.read())
                self.assertTrue(run_result["ok"])
                self.assertEqual(
                    run_result["state"]["active"]["effective_method"],
                    "random_projection",
                )

                request = urllib.request.Request(
                    f"{base_url}/api/selfcheck",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request) as response:
                    check_result = json.loads(response.read())
                self.assertTrue(check_result["ok"])
                self.assertTrue(check_result["selfcheck"]["overall_passed"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
