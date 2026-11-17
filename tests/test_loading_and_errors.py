"""需求 1：加载层校验与 E_TIMESERIES_INVALID / E_CONFIG_INVALID。"""
from __future__ import annotations

import os
import tempfile
import unittest

from vbench.config import ConfigError, load_config
from vbench.errors import ErrorCode
from vbench.samples import load_clips

from ._harness import fresh_workspace


class LoadingValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="vbench-load-")
        self.ws = fresh_workspace(self.temp_dir, "ws", clip_count=3,
                                  frames=5, denoise_steps=6)

    def test_valid_sample_loads(self) -> None:
        config = self.ws.config()
        clips, failures = load_clips(self.ws.sample_dir, config)
        self.assertEqual(len(clips), 3)
        self.assertEqual(failures, [])
        self.assertEqual(len(clips[0].frames), 5)
        self.assertEqual(len(clips[0].labels), 5)
        self.assertEqual(set(clips[0].init_images), set(config.tier_names))

    def test_config_missing_field(self) -> None:
        with self.assertRaises(ConfigError) as context:
            self.ws.config("fault_missing_field")
        self.assertIn("guidance_scale", str(context.exception))

    def test_config_bad_table_length(self) -> None:
        with self.assertRaises(ConfigError):
            self.ws.config("fault_bad_table_len")

    def test_config_bad_guidance(self) -> None:
        with self.assertRaises(ConfigError):
            self.ws.config("fault_bad_guidance")

    def test_config_non_monotonic_tiers(self) -> None:
        with self.assertRaises(ConfigError):
            self.ws.config("fault_non_monotonic")

    def test_frame_mismatch_is_timeseries_error(self) -> None:
        config = self.ws.config()
        sample_dir = self.ws.faults["frame_mismatch"]
        clips, failures = load_clips(sample_dir, config)
        codes = {item.code for item in failures}
        self.assertIn(ErrorCode.E_TIMESERIES_INVALID, codes)
        # 其余合法片段仍可进入渲染。
        self.assertEqual(len(clips), 2)

    def test_duplicate_id_is_timeseries_error(self) -> None:
        config = self.ws.config()
        sample_dir = self.ws.faults["duplicate_id"]
        clips, failures = load_clips(sample_dir, config)
        self.assertTrue(any(
            item.code == ErrorCode.E_TIMESERIES_INVALID
            and "重复" in item.message for item in failures))
        self.assertLess(len(clips), 3)

    def test_zero_budget_config_still_loads(self) -> None:
        config = self.ws.config("fault_zero_budget")
        self.assertEqual(config.denoise_step_budget, 0)
        clips, failures = load_clips(self.ws.faults["zero_budget"], config)
        self.assertEqual(len(clips), 3)
        self.assertEqual(failures, [])

    def test_invalid_config_emits_business_code_in_report(self) -> None:
        from vbench.cli import _write_report
        out = os.path.join(self.temp_dir, "bad-report")
        os.makedirs(out, exist_ok=True)
        try:
            self.ws.config("fault_bad_guidance")
        except ConfigError as exc:
            path = _write_report(out, {
                "code": ErrorCode.E_CONFIG_INVALID, "message": str(exc)})
        import json
        with open(path, encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["code"], ErrorCode.E_CONFIG_INVALID)


if __name__ == "__main__":
    unittest.main()
