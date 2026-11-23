"""Requirement 8: structured validation codes and resource limits."""

from __future__ import annotations

import copy
import tempfile
import unittest

from calib_workbench.config import validate_config
from calib_workbench.errors import (
    E_BATCH_LIMIT,
    E_DUP_SAMPLE_ID,
    E_FEATURE_LENGTH,
    E_GROUP_MISSING,
    E_INVALID_BINS,
    E_INVALID_SPLIT_RATIO,
    E_MEM_TABLE_LIMIT,
    E_PARALLEL_LIMIT,
    E_UNKNOWN_CALIBRATOR,
    WorkbenchError,
)
from calib_workbench.pipeline import run_pipeline
from calib_workbench.validate import validate_manifest

from tests.helpers import small_cfg, small_rows


class TestManifestValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = small_cfg(f"{self.tmp.name}/s", f"{self.tmp.name}/o")
        self.rows = small_rows(self.cfg)

    def tearDown(self):
        self.tmp.cleanup()

    def _expect(self, rows, code):
        with self.assertRaises(WorkbenchError) as ctx:
            validate_manifest(rows, self.cfg)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsNotNone(ctx.exception.field)

    def test_duplicate_sample_id(self):
        rows = copy.deepcopy(self.rows)
        rows[1]["sample_id"] = rows[0]["sample_id"]
        self._expect(rows, E_DUP_SAMPLE_ID)

    def test_feature_length_mismatch(self):
        rows = copy.deepcopy(self.rows)
        del rows[0]["numeric"]["f3"]
        self._expect(rows, E_FEATURE_LENGTH)

    def test_group_missing(self):
        rows = copy.deepcopy(self.rows)
        rows[0]["group"] = ""
        self._expect(rows, E_GROUP_MISSING)

    def test_valid_manifest_passes(self):
        validate_manifest(self.rows, self.cfg)


class TestConfigValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = small_cfg(f"{self.tmp.name}/s", f"{self.tmp.name}/o")

    def tearDown(self):
        self.tmp.cleanup()

    def _expect(self, mutate, code):
        cfg = copy.deepcopy(self.cfg)
        mutate(cfg)
        with self.assertRaises(WorkbenchError) as ctx:
            validate_config(cfg)
        self.assertEqual(ctx.exception.code, code)
        self.assertIsNotNone(ctx.exception.field)

    def test_unknown_calibrator(self):
        self._expect(lambda c: c["calibrators"].update({"svm": {}}), E_UNKNOWN_CALIBRATOR)

    def test_invalid_bins(self):
        self._expect(lambda c: c["calibrators"]["histogram"].update({"n_bins": 1}),
                     E_INVALID_BINS)

    def test_invalid_split_ratio(self):
        self._expect(lambda c: c["split"].update({"ref_ratio": 1.5}), E_INVALID_SPLIT_RATIO)

    def test_batch_limit(self):
        self._expect(lambda c: c["scheduler"].update({"batch_size": 99}), E_BATCH_LIMIT)

    def test_parallel_limit(self):
        self._expect(lambda c: c["limits"].update({"max_parallel_combos": 2}),
                     E_PARALLEL_LIMIT)


class TestMemTableLimit(unittest.TestCase):
    def test_prob_table_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = small_cfg(f"{tmp}/s", f"{tmp}/o")
            cfg["limits"]["max_prob_table_bytes"] = 8  # absurdly small
            rows = small_rows(cfg)
            with self.assertRaises(WorkbenchError) as ctx:
                run_pipeline(cfg, rows=rows)
            self.assertEqual(ctx.exception.code, E_MEM_TABLE_LIMIT)
            self.assertEqual(ctx.exception.field, "limits.max_prob_table_bytes")


if __name__ == "__main__":
    unittest.main()
