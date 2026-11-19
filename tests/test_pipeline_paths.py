import json
import os
import tempfile
import unittest

from voice_dialogue.cli import main
from voice_dialogue.config import ComponentConfig, PathProfile, ResourceLimits
from voice_dialogue.errors import BusinessError, ErrorCode
from voice_dialogue.pipeline import run_pipeline
from voice_dialogue.selector import observe_scale, decide_path
from voice_dialogue.synth import generate_sample_bundle


class PathSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.tmp, "samples")
        self.manifest = generate_sample_bundle(
            self.data_dir, small_dialogs=2, large_dialogs=3
        )

    def _rows(self, prefix):
        rows = []
        with open(self.manifest, encoding="utf-8") as fh:
            for line in fh:
                row = json.loads(line)
                if row["dialog_id"].startswith(prefix):
                    rows.append(row)
        path = os.path.join(self.data_dir, f"manifest_{prefix}.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, sort_keys=True) + "\n")
        return path

    def test_small_uses_fine_large_uses_approximate(self):
        config = ComponentConfig()
        small = self._rows("small")
        report = run_pipeline(small, config)
        self.assertEqual(report["path_decision"]["path"], "fine")
        self.assertEqual(report["path_decision"]["frame_stride"], 1)
        self.assertEqual(report["dialogs"][0]["feature"]["dim"], 8)

        large = self._rows("large")
        report_big = run_pipeline(large, config)
        self.assertEqual(
            report_big["path_decision"]["path"], "approximate"
        )
        self.assertEqual(report_big["dialogs"][0]["feature"]["dim"], 4)

    def test_resident_budget_forces_frame_stride(self):
        config = ComponentConfig(
            resource_limits=ResourceLimits(
                max_resident_frames=1500,
                max_separation_comparisons=50_000_000,
                max_regression_terms=5_000_000,
                on_exceed="degrade",
            )
        )
        report = run_pipeline(self._rows("large"), config)
        self.assertEqual(report["path_decision"]["path"], "approximate")
        self.assertGreaterEqual(report["path_decision"]["frame_stride"], 2)
        self.assertIn(
            "FRAME_STRIDE_APPROXIMATION",
            report["path_decision"]["degradation"],
        )
        # Frame boundaries remain the full GT-aligned set.
        with open(self._rows("large"), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        for dialog, row in zip(report["dialogs"], rows):
            self.assertEqual(dialog["frame_count"], row["num_frames"])

    def test_decision_is_deterministic(self):
        config = ComponentConfig()
        with open(self.manifest, encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        obs = observe_scale(rows, config)
        d1 = decide_path(obs, config).to_dict()
        d2 = decide_path(obs, config).to_dict()
        self.assertEqual(d1, d2)

    def test_report_byte_identical_across_runs(self):
        config = ComponentConfig()
        small = self._rows("small")
        r1 = os.path.join(self.tmp, "r1.json")
        r2 = os.path.join(self.tmp, "r2.json")
        run_pipeline(small, config, report_path=r1)
        run_pipeline(small, config, report_path=r2)
        with open(r1, "rb") as fh:
            r1_bytes = fh.read()
        with open(r2, "rb") as fh:
            r2_bytes = fh.read()
        self.assertEqual(r1_bytes, r2_bytes)

    def test_resource_budget_error_path(self):
        config = ComponentConfig(
            resource_limits=ResourceLimits(
                max_resident_frames=10,
                max_separation_comparisons=10,
                max_regression_terms=10,
                on_exceed="error",
            )
        )
        with self.assertRaises(BusinessError) as ctx:
            run_pipeline(self._rows("large"), config)
        self.assertEqual(ctx.exception.code, ErrorCode.E_RESOURCE_LIMIT)

    def test_resource_budget_degrade_path(self):
        config = ComponentConfig(
            path_profile=PathProfile(
                max_fine_dialogs=100,
                max_fine_total_frames=10_000_000,
                max_fine_speakers=100,
                max_fine_feature_dim=100,
            ),
            resource_limits=ResourceLimits(
                max_resident_frames=500,
                max_separation_comparisons=1_000,
                max_regression_terms=100_000,
                on_exceed="degrade",
            ),
        )
        report = run_pipeline(self._rows("small"), config)
        self.assertEqual(
            report["path_decision"]["path"], "approximate"
        )
        self.assertTrue(report["path_decision"]["degradation"])
        # Frame boundaries must remain unchanged.
        with open(self._rows("small"), encoding="utf-8") as fh:
            rows = [json.loads(l) for l in fh]
        for dialog, row in zip(report["dialogs"], rows):
            self.assertEqual(dialog["frame_count"], row["num_frames"])

    def test_cli_exit_code_on_business_error(self):
        rc = main([
            "run",
            "--manifest", os.path.join(self.tmp, "missing.jsonl"),
            "--report", os.path.join(self.tmp, "err.json"),
        ])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
