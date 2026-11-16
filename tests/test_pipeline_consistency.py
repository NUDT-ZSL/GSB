import copy
import json
import os
import shutil
import unittest

from featbench.config import load_config
from featbench.pipeline import run_pipeline
from featbench.snapshot import ShutdownFlag
from tests._util import BASE_CONFIG, TempWorkdir, write_config
import yaml


COMPARABLE_KEYS = ("metric_table", "retained_features", "decisions",
                   "cross_budget_audit", "failures", "scales", "methods")


def _read(config_path):
    with open(config_path, encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _write_raw(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, sort_keys=True)


def _comparable(report):
    return {key: report[key] for key in COMPARABLE_KEYS}


class IncrementalConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TempWorkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_edit_sequence_matches_clean_full_run(self):
        config_path = write_config(self.tmp.path, {
            "scales": [
                {"id": "small", "samples": 60, "feature_multiplier": 2.0},
                {"id": "medium", "samples": 400,
                 "feature_multiplier": 1.0,
                 "cardinality_multiplier": 2.0},
            ],
            "workers": 4,
        })
        run1 = run_pipeline(load_config(config_path))

        payload = _read(config_path)
        # Edit 1: scale switch (medium sample size grows).
        payload["scales"][1]["samples"] = 500
        _write_raw(config_path, payload)
        run_pipeline(load_config(config_path))

        # Edit 2: cross order adjustment.
        payload = _read(config_path)
        payload["cross"]["max_order"] = 1
        _write_raw(config_path, payload)
        run_pipeline(load_config(config_path))

        # Edit 3: cardinality budget change + degrade strategy switch.
        payload = _read(config_path)
        payload["cross"]["cardinality_budget"] = 8
        payload["cross"]["degrade_strategy"] = "skip"
        _write_raw(config_path, payload)
        run_pipeline(load_config(config_path))

        # Edit 4: reduction method switch (drop pca, keep others).
        payload = _read(config_path)
        payload["reduction"]["methods"] = ["variance", "random_projection"]
        payload["reduction"]["target_dim"] = 6
        _write_raw(config_path, payload)
        run_pipeline(load_config(config_path))
        # Re-running the unchanged final config must hit the cache instead of
        # recomputing any stage, while yielding the identical report.
        incremental = run_pipeline(load_config(config_path))

        clean_dir = os.path.join(self.tmp.path, "clean")
        os.makedirs(clean_dir, exist_ok=True)
        clean_path = os.path.join(clean_dir, "final.yaml")
        final_payload = copy.deepcopy(payload)
        clean_work = os.path.join(clean_dir, "fresh_work")
        shutil.rmtree(clean_work, ignore_errors=True)
        final_payload["work_dir"] = clean_work
        _write_raw(clean_path, final_payload)
        full = run_pipeline(load_config(clean_path))

        self.assertEqual(_comparable(incremental), _comparable(full))
        self.assertEqual(incremental["report_fingerprint"],
                         full["report_fingerprint"])
        # Incremental run must have reused reduction/evaluation artifacts too.
        self.assertTrue(any(key.startswith(("reduce__", "evaluate__"))
                            for key in incremental["cache_ledger"]["reused"]))
        # A clean full run still reuses only the per-scale shared cross/encode
        # stages across method tasks; reduction and evaluation are computed once.
        full_shared_hits = [
            key for key in full["cache_ledger"]["reused"]
            if key.startswith(("cross__", "encode__"))]
        full_path_hits = [
            key for key in full["cache_ledger"]["reused"]
            if key.startswith(("reduce__", "evaluate__"))]
        self.assertTrue(full_shared_hits)
        self.assertEqual(full_path_hits, [])

    def test_concurrent_and_serial_orders_match(self):
        path_serial = write_config(os.path.join(self.tmp.path, "serial"), {
            "workers": 1}) if os.path.isdir(os.path.join(self.tmp.path, "serial")) \
            else None
        serial_dir = os.path.join(self.tmp.path, "serial")
        os.makedirs(serial_dir, exist_ok=True)
        serial_path = os.path.join(serial_dir, "config.yaml")
        payload = copy.deepcopy(BASE_CONFIG)
        payload["work_dir"] = os.path.join(serial_dir, "work")
        payload["workers"] = 1
        _write_raw(serial_path, payload)

        parallel_dir = os.path.join(self.tmp.path, "parallel")
        os.makedirs(parallel_dir, exist_ok=True)
        parallel_path = os.path.join(parallel_dir, "config.yaml")
        payload["work_dir"] = os.path.join(parallel_dir, "work")
        payload["workers"] = 4
        _write_raw(parallel_path, payload)

        serial = run_pipeline(load_config(serial_path))
        parallel = run_pipeline(load_config(parallel_path))
        self.assertEqual([row["sort_key"] for row in serial["metric_table"]],
                         [row["sort_key"] for row in parallel["metric_table"]])
        self.assertEqual(_comparable(serial), _comparable(parallel))


if __name__ == "__main__":
    unittest.main()
