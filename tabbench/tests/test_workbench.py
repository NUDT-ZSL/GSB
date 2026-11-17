"""End-to-end and unit tests for the tabbench workbench."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import yaml

from tools.compare_runs import normalize, diff_paths
from workbench.data import fingerprint, fold_indices, generate_data
from workbench.errors import (E_BUDGET_EXHAUSTED, E_CONFIG_INVALID,
                              E_CONFIG_MISMATCH, E_TASK_TIMEOUT)
from workbench.runner import run_workbench

DATA_DESC = {
    "dataset_name": "test",
    "seed": 424242,
    "n_samples": 90,
    "target": "label",
    "missing_rate": 0.05,
    "imbalance_ratio": 0.3,
    "resample_target_ratio": 1.0,
    "columns": [
        {"name": "a", "type": "numeric", "mean": 10.0, "std": 2.0},
        {"name": "b", "type": "numeric", "mean": 0.0, "std": 1.0},
        {"name": "c", "type": "categorical", "categories": ["x", "y", "z"]},
    ],
}


def make_experiment(**sched_overrides):
    sched = {
        "max_concurrency": 2,
        "global_budget_ms": 1000000,
        "task_quota_ms": 1000000,
        "seeds": [7, 11],
        "snapshot_interval": 2,
    }
    sched.update(sched_overrides)
    return {
        "experiment_name": "test",
        "data_file": "data.yaml",
        "cv_folds": 3,
        "model_grid": {
            "logistic": {"learning_rate": [0.2], "epochs": [12, 24]},
            "tree": {"max_depth": [3], "min_samples_split": [4]},
            "knn": {"k": [3]},
        },
        "scheduler": sched,
        "limits": {"max_cardinality": 8},
    }


class Workspace:
    def __init__(self, experiment, data_desc):
        self.dir = tempfile.mkdtemp(prefix="tabbench_test_")
        self.config = os.path.join(self.dir, "experiment.yaml")
        self.data = os.path.join(self.dir, "data.yaml")
        with open(self.config, "w", encoding="utf-8") as fh:
            yaml.safe_dump(experiment, fh)
        with open(self.data, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data_desc, fh)

    def out(self, name):
        return os.path.join(self.dir, name)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def run(ws, out_name, **kwargs):
    buf = io.StringIO()
    code = run_workbench(ws.config, ws.data, ws.out(out_name), out=buf,
                         **kwargs)
    return code, buf.getvalue()


def load_report(ws, out_name):
    with open(os.path.join(ws.out(out_name), "report.json"),
              encoding="utf-8") as fh:
        return json.load(fh)


class DataLayerTest(unittest.TestCase):
    def test_data_generation_deterministic(self):
        ds1 = generate_data(DATA_DESC)
        ds2 = generate_data(DATA_DESC)
        self.assertEqual(fingerprint(ds1), fingerprint(ds2))

    def test_imbalance_resampled(self):
        ds = generate_data(DATA_DESC)
        self.assertEqual(ds.y.count(0), ds.y.count(1))

    def test_folds_non_overlapping(self):
        folds = fold_indices(97, 3, 7)
        seen = sorted(i for f in folds for i in f)
        self.assertEqual(seen, list(range(97)))

    def test_folds_deterministic(self):
        self.assertEqual(fold_indices(50, 3, 7), fold_indices(50, 3, 7))


class ConfigValidationTest(unittest.TestCase):
    def _run_invalid(self, mutate):
        desc = json.loads(json.dumps(DATA_DESC))
        mutate(desc)
        ws = Workspace(make_experiment(), desc)
        try:
            code, output = run(ws, "out_invalid")
            report = load_report(ws, "out_invalid")
        finally:
            ws.cleanup()
        self.assertEqual(code, 0)
        codes = [f["code"] for f in report["failures"]]
        self.assertIn(E_CONFIG_INVALID, codes)
        return report

    def test_seed_not_integer(self):
        report = self._run_invalid(lambda d: d.update(seed="abc"))
        details = " ".join(f["detail"] for f in report["failures"])
        self.assertIn("seed_not_integer", details)

    def test_unknown_feature_column(self):
        report = self._run_invalid(
            lambda d: d.update(feature_columns=["a", "ghost"]))
        details = " ".join(f["detail"] for f in report["failures"])
        self.assertIn("unknown_column", details)

    def test_target_missing(self):
        report = self._run_invalid(lambda d: d.pop("target"))
        details = " ".join(f["detail"] for f in report["failures"])
        self.assertIn("target_missing", details)

    def test_type_mismatch(self):
        report = self._run_invalid(
            lambda d: d["columns"][0].update(mean="ten"))
        details = " ".join(f["detail"] for f in report["failures"])
        self.assertIn("type_mismatch", details)

    def test_cardinality_exceeded(self):
        def mutate(d):
            d["columns"][2]["categories"] = ["c%02d" % i for i in range(20)]
        report = self._run_invalid(mutate)
        details = " ".join(f["detail"] for f in report["failures"])
        self.assertIn("cardinality_exceeded", details)


class ConcurrencyConsistencyTest(unittest.TestCase):
    def test_serial_vs_parallel_identical(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            code1, out1 = run(ws, "c1", max_concurrency=1)
            code3, out3 = run(ws, "c3", max_concurrency=3)
            self.assertEqual(code1, 0)
            self.assertEqual(code3, 0)
            rep1 = normalize(load_report(ws, "c1"))
            rep3 = normalize(load_report(ws, "c3"))
            diffs = diff_paths(rep1, rep3)
            self.assertEqual(diffs, [])
            summary1 = [l for l in out1.splitlines()
                        if l.startswith("SUMMARY|")]
            summary3 = [l for l in out3.splitlines()
                        if l.startswith("SUMMARY|")]
            self.assertEqual(summary1, summary3)
            self.assertTrue(summary1[0])
        finally:
            ws.cleanup()

    def test_repeat_run_identical(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            run(ws, "r1", max_concurrency=2)
            run(ws, "r2", max_concurrency=2)
            rep1 = normalize(load_report(ws, "r1"))
            rep2 = normalize(load_report(ws, "r2"))
            self.assertEqual(diff_paths(rep1, rep2), [])
        finally:
            ws.cleanup()


class BudgetControlTest(unittest.TestCase):
    def test_global_budget_exhaustion(self):
        exp = make_experiment(global_budget_ms=400)
        ws = Workspace(exp, DATA_DESC)
        try:
            code, _ = run(ws, "out_budget")
            self.assertEqual(code, 0)
            report = load_report(ws, "out_budget")
            self.assertTrue(report["budget_exhausted"])
            for key in report["budget_exhausted"]:
                entry = [t for t in report["tasks"]
                         if t["task_key"] == key][0]
                self.assertEqual(entry["code"], E_BUDGET_EXHAUSTED)
                self.assertEqual(entry["status"], "exhausted")
            completed = [t for t in report["tasks"]
                         if t["status"] in ("ok", "timeout")]
            self.assertTrue(completed)
            self.assertLessEqual(report["budget"]["consumed_ms"],
                                 report["budget"]["global_ms"])
        finally:
            ws.cleanup()

    def test_task_quota_timeout_keeps_partial_losses(self):
        exp = make_experiment(task_quota_ms=5)
        ws = Workspace(exp, DATA_DESC)
        try:
            code, _ = run(ws, "out_quota")
            self.assertEqual(code, 0)
            report = load_report(ws, "out_quota")
            lr_tasks = [t for t in report["tasks"]
                        if t["model"] == "logistic"]
            self.assertTrue(lr_tasks)
            for task in lr_tasks:
                self.assertTrue(task["timed_out"])
                self.assertEqual(task["code"], E_TASK_TIMEOUT)
                epochs = task["params"]["epochs"]
                for fold_losses in task["losses"]:
                    self.assertLess(len(fold_losses), epochs)
                    self.assertGreater(len(fold_losses), 0)
        finally:
            ws.cleanup()


class SnapshotResumeTest(unittest.TestCase):
    def test_resume_matches_uninterrupted(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            code, _ = run(ws, "full")
            self.assertEqual(code, 0)
            # interrupted run: halt after 2 tasks, then resume
            code, out_int = run(ws, "resumed", halt_after=2)
            self.assertEqual(code, 0)
            self.assertIn("INTERRUPTED|", out_int)
            self.assertFalse(os.path.exists(
                os.path.join(ws.out("resumed"), "report.json")))
            code, out_res = run(ws, "resumed")
            self.assertEqual(code, 0)
            self.assertIn("RESTORE|", out_res)
            rep_full = normalize(load_report(ws, "full"))
            rep_res = normalize(load_report(ws, "resumed"))
            self.assertEqual(diff_paths(rep_full, rep_res), [])
        finally:
            ws.cleanup()

    def test_config_mismatch_refused(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            code, _ = run(ws, "out", halt_after=2)
            self.assertEqual(code, 0)
            # mutate the config -> hash changes -> resume refused
            with open(ws.config, "a", encoding="utf-8") as fh:
                fh.write("extra_key: 1\n")
            code, output = run(ws, "out")
            self.assertEqual(code, 2)
            self.assertIn(E_CONFIG_MISMATCH, output)
        finally:
            ws.cleanup()

    def test_snapshot_atomic_no_tmp_left(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            run(ws, "out")
            leftovers = []
            for root, _, files in os.walk(ws.out("out")):
                leftovers.extend(f for f in files if f.endswith(".tmp"))
            self.assertEqual(leftovers, [])
            with open(os.path.join(ws.out("out"), "snapshot.json"),
                      encoding="utf-8") as fh:
                snap = json.load(fh)
            self.assertEqual(snap["phase"], "done")
            self.assertIn("config_hash", snap)
            self.assertIn("data_fingerprint", snap)
            self.assertIn("budget_consumed_ms", snap)
        finally:
            ws.cleanup()


class ReportContentTest(unittest.TestCase):
    def test_report_fields(self):
        ws = Workspace(make_experiment(), DATA_DESC)
        try:
            code, output = run(ws, "out")
            self.assertEqual(code, 0)
            report = load_report(ws, "out")
            self.assertIn("config_hash", report)
            self.assertIn("snapshot_version", report)
            self.assertIn("decision_log", report)
            self.assertTrue(report["decision_log"])
            self.assertTrue(report["completion_order"])
            for task in report["tasks"]:
                self.assertIn("params", task)
                self.assertIn("enqueue_seq", task)
                if task["status"] == "ok":
                    self.assertTrue(task["folds"])
                    for fold in task["folds"]:
                        self.assertIn("accuracy", fold)
                        self.assertIn("confusion", fold)
                        self.assertIn("predictions", fold)
                        self.assertIn("sort_key", fold)
            summary = [l for l in output.splitlines()
                       if l.startswith("SUMMARY|")]
            self.assertEqual(len(summary), 1)
            self.assertEqual(summary[0], report["summary_line"])
        finally:
            ws.cleanup()


if __name__ == "__main__":
    unittest.main()
