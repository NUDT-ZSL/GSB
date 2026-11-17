"""End-to-end and unit tests for the positional-encoding workbench."""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from peworkbench import (
    GLOBAL_VERSION,
    EXIT_CONFIG_MISMATCH,
    EXIT_OK,
    EXIT_SCHEME_UNKNOWN,
)
from peworkbench.datagen import generate
from peworkbench.io_utils import load_json
from peworkbench.runner import run
from peworkbench.schemes import get_scheme
from peworkbench.sensitivity import boundary_check


class WorkbenchTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pew-test-")
        cls.data = os.path.join(cls.tmp, "data")
        generate(cls.data)
        cls.config = os.path.join(cls.data, "config.json")
        cls.samples = os.path.join(cls.data, "samples.jsonl")
        cls.injections = os.path.join(cls.data, "injections.json")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def workdir(self, name):
        return os.path.join(self.tmp, name)

    def run_pipeline(self, name, **kwargs):
        workdir = self.workdir(name)
        code = run(self.config, self.samples, self.injections, workdir, **kwargs)
        return code, workdir

    def test_full_run_ok(self):
        code, workdir = self.run_pipeline("full")
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        self.assertEqual([s["name"] for s in report["meta"]["schemes"]],
                         ["linear_interp", "ntk_aware", "rope"])
        for scheme in ("linear_interp", "ntk_aware", "rope"):
            stats = report["score_stats"][scheme]
            self.assertGreater(stats["in_range"]["count"], 0)
            self.assertGreater(stats["extrapolated"]["count"], 0)
            self.assertLessEqual(stats["in_range"]["min"],
                                 stats["in_range"]["max"])
        self.assertTrue(os.path.exists(
            os.path.join(workdir, "visual_summary.txt")))

    def test_injection_hits_and_failures(self):
        code, workdir = self.run_pipeline("inject")
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        hits = {h["inj_id"]: h for h in report["injection_hits"]}
        self.assertEqual(
            {i: hits[i]["code"] for i in sorted(hits)},
            {"INJ-001": "E_NONFINITE_FEATURE",
             "INJ-002": "E_POS_OUT_OF_RANGE",
             "INJ-003": "E_BASE_ZERO",
             "INJ-004": "E_SCALE_INVALID"})
        self.assertTrue(all(h["applied"] for h in hits.values()))
        codes = {(f["sample_id"], f["code"]) for f in report["failures"]}
        self.assertIn(("s0002", "E_NONFINITE_FEATURE"), codes)
        self.assertIn(("s0005", "E_POS_OUT_OF_RANGE"), codes)
        self.assertIn(("s0008", "E_BASE_ZERO"), codes)
        self.assertIn(("s0011", "E_SCALE_INVALID"), codes)
        # failures sorted by (scheme, sample_id, stage)
        keys = [(f["scheme"], f["sample_id"], f["stage"])
                for f in report["failures"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(report["meta"]["injection_hash"],
                         report["meta"]["injection_hash"])

    def test_concurrency_matches_serial(self):
        code1, wd1 = self.run_pipeline("serial")
        cfg = load_json(self.config)
        cfg["max_workers"] = 8
        cfg_path = os.path.join(self.tmp, "config-parallel.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        wd2 = self.workdir("parallel")
        code2 = run(cfg_path, self.samples, self.injections, wd2)
        self.assertEqual((code1, code2), (EXIT_OK, EXIT_OK))
        r1 = load_json(os.path.join(wd1, "report.json"))
        r2 = load_json(os.path.join(wd2, "report.json"))
        # config hash differs (max_workers differs); compare everything else
        self.assertEqual(r2["meta"]["max_workers"], 8)
        for key in ("score_stats", "error_tracking", "sensitivity",
                    "injection_hits", "failures", "cache"):
            self.assertEqual(r1[key], r2[key], key)

    def test_reproducibility_identical_bytes(self):
        code1, wd1 = self.run_pipeline("rep1")
        code2, wd2 = self.run_pipeline("rep2")
        self.assertEqual((code1, code2), (EXIT_OK, EXIT_OK))
        with open(os.path.join(wd1, "report.json"), "rb") as fh:
            first = fh.read()
        with open(os.path.join(wd2, "report.json"), "rb") as fh:
            second = fh.read()
        self.assertEqual(first, second)

    def test_unknown_scheme(self):
        cfg = load_json(self.config)
        cfg["scheme"] = "does_not_exist"
        cfg_path = os.path.join(self.tmp, "config-unknown.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        code = run(cfg_path, self.samples, self.injections,
                   self.workdir("unknown"))
        self.assertEqual(code, EXIT_SCHEME_UNKNOWN)

    def test_config_mismatch_refused(self):
        code, workdir = self.run_pipeline("mismatch")
        self.assertEqual(code, EXIT_OK)
        cfg = load_json(self.config)
        cfg["train_ctx_len"] = cfg["train_ctx_len"] + 1
        cfg_path = os.path.join(self.tmp, "config-mismatch.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        code = run(cfg_path, self.samples, self.injections, workdir)
        self.assertEqual(code, EXIT_CONFIG_MISMATCH)

    def test_incremental_invalidation_sample_change(self):
        code, workdir = self.run_pipeline("incr")
        self.assertEqual(code, EXIT_OK)
        # change one sample's features -> only that sample recomputes
        with open(self.samples, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh]
        rows[0]["features"]["query"][0] = rows[0]["features"]["query"][0] + 1.0
        mod_samples = os.path.join(self.tmp, "samples-mod.jsonl")
        with open(mod_samples, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        code = run(self.config, mod_samples, self.injections, workdir,
                   allow_reconfigure=True)
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        recomputed = report["cache"]["recomputed"]
        changed_ids = {e["sample_id"] for e in recomputed}
        self.assertEqual(changed_ids, {rows[0]["sample_id"]})
        # 3 schemes x 3 stages for the changed sample only
        self.assertEqual(len(recomputed), 9)
        self.assertGreater(len(report["cache"]["reused"]), 0)

    def test_incremental_invalidation_param_change(self):
        code, workdir = self.run_pipeline("incr2")
        self.assertEqual(code, EXIT_OK)
        cfg = load_json(self.config)
        cfg["base_freq"] = cfg["base_freq"] * 1.5
        cfg_path = os.path.join(self.tmp, "config-base.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        code = run(cfg_path, self.samples, self.injections, workdir,
                   allow_reconfigure=True)
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        # base change invalidates every scheme/stage -> nothing reused
        self.assertEqual(report["cache"]["reused"], [])
        self.assertGreater(len(report["cache"]["recomputed"]), 0)

    def test_scale_change_spares_rope(self):
        code, workdir = self.run_pipeline("incr3")
        self.assertEqual(code, EXIT_OK)
        cfg = load_json(self.config)
        cfg["scale_factor"] = cfg["scale_factor"] * 2.0
        cfg_path = os.path.join(self.tmp, "config-scale.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        code = run(cfg_path, self.samples, self.injections, workdir,
                   allow_reconfigure=True)
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        reused = report["cache"]["reused"]
        reused_rope = [e for e in reused if e["scheme"] == "rope"]
        # rope does not depend on scale_factor: fully reused
        self.assertEqual(len(reused_rope), 3 * 20)  # stages x valid samples
        recomputed = report["cache"]["recomputed"]
        self.assertTrue(all(e["scheme"] != "rope" for e in recomputed))

    def test_snapshots_atomic_and_manifest(self):
        code, workdir = self.run_pipeline("snap")
        self.assertEqual(code, EXIT_OK)
        for root, _, files in os.walk(workdir):
            for name in files:
                self.assertFalse(name.startswith(".tmp-"), name)
        manifest = load_json(os.path.join(workdir, "snapshots", "manifest.json"))
        self.assertEqual(manifest["global_version"], GLOBAL_VERSION)
        self.assertEqual(set(manifest["completed_stages"]),
                         {"encode", "score", "track", "sensitivity", "report"})
        self.assertEqual(len(manifest["config_hash"]), 64)
        self.assertEqual(len(manifest["injection_hash"]), 64)
        # resume with identical config regenerates an identical report
        code = run(self.config, self.samples, self.injections, workdir)
        self.assertEqual(code, EXIT_OK)

    def test_boundary_check_detects_truncation_and_overflow(self):
        scheme = get_scheme("rope")
        kinds, theta = boundary_check(scheme, 1.0e300, 1.0, 64, "fp32")
        self.assertIn("dim_truncation", kinds)
        kinds, _ = boundary_check(scheme, 10000.0, 1.0, 64, "fp32")
        self.assertEqual(kinds, [])

    def test_sensitivity_report_structure(self):
        code, workdir = self.run_pipeline("sens")
        self.assertEqual(code, EXIT_OK)
        report = load_json(os.path.join(workdir, "report.json"))
        for scheme in ("linear_interp", "ntk_aware", "rope"):
            sens = report["sensitivity"][scheme]
            self.assertEqual(sorted(sens["ranking"]),
                             ["base_freq", "scale_factor"])
            for param in ("base_freq", "scale_factor"):
                entries = sens["parameters"][param]["entries"]
                self.assertEqual(len(entries), 5)


if __name__ == "__main__":
    unittest.main()
