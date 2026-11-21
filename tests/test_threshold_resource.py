from __future__ import annotations

import copy
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from dimdiag.config import validate_config
from dimdiag.diagnostics import stable_threshold_decision
from dimdiag.engine import DiagnosticEngine
from dimdiag.errors import E_CONFIG_INVALID, E_RESOURCE_LIMIT, BusinessError

from .helpers import base_config, prepare_workspace


class ThresholdAndResourceTests(unittest.TestCase):
    def test_tolerance_band_does_not_flip_near_threshold(self):
        config = validate_config(
            {"dataset": {"numeric_columns": ["x"], "categorical_columns": []}}
        )
        config["evaluation"]["threshold_strategy"] = "tolerance_band"
        config["evaluation"]["threshold_tolerance"] = 0.01
        threshold = 0.8
        below = float(np.nextafter(threshold, 0.0))
        above = float(np.nextafter(threshold, 1.0))
        low_trace = stable_threshold_decision(
            stage="test",
            value=below,
            threshold=threshold,
            config=config,
        )
        high_trace = stable_threshold_decision(
            stage="test",
            value=above,
            threshold=threshold,
            config=config,
        )
        self.assertEqual(low_trace["decision"], high_trace["decision"])
        self.assertTrue(low_trace["stability_guaranteed"])
        self.assertTrue(high_trace["stability_guaranteed"])

        strict = copy.deepcopy(config)
        strict["evaluation"]["threshold_strategy"] = "strict"
        strict_low = stable_threshold_decision(
            stage="test",
            value=below,
            threshold=threshold,
            config=strict,
        )
        strict_high = stable_threshold_decision(
            stage="test",
            value=above,
            threshold=threshold,
            config=strict,
        )
        self.assertFalse(strict_low["decision"])
        self.assertTrue(strict_high["decision"])
        self.assertFalse(strict_low["stability_guaranteed"])

    def test_invalid_threshold_tolerance(self):
        config = validate_config(
            {"dataset": {"numeric_columns": ["x"], "categorical_columns": []}}
        )
        config["evaluation"]["threshold_tolerance"] = -1.0
        with self.assertRaises(BusinessError) as raised:
            validate_config(config)
        self.assertEqual(raised.exception.code, E_CONFIG_INVALID)

    def test_resource_limit_error(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["resources"]["max_features"] = 3
            with self.assertRaises(BusinessError) as raised:
                DiagnosticEngine(root, base_dir=config_path.parent).run(
                    config, base_dir=config_path.parent
                )
            self.assertEqual(raised.exception.code, E_RESOURCE_LIMIT)
            self.assertEqual(raised.exception.details["reason"], "max_features")

    def test_resource_degradation_is_marked_and_reproducible(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = prepare_workspace(root)
            config = base_config(config_path)
            config["resources"]["max_dim_matrix_bytes"] = 2000
            config["resources"]["overflow_policy"] = "degrade_random_projection"
            engine = DiagnosticEngine(root, base_dir=config_path.parent)
            first = engine.run(config, base_dir=config_path.parent)
            self.assertEqual(first.observed.requested_method, "pca")
            self.assertEqual(first.observed.effective_method, "random_projection")
            self.assertTrue(first.observed.resource_plan["degraded"])
            self.assertEqual(
                first.observed.resource_plan["degradation"]["category"],
                "pca_to_random_projection",
            )
            second = engine.run(config, base_dir=config_path.parent)
            self.assertEqual(
                first.observed.coordinate_checksum(),
                second.observed.coordinate_checksum(),
            )
            self.assertTrue(second.selfcheck["overall_passed"])


if __name__ == "__main__":
    unittest.main()

