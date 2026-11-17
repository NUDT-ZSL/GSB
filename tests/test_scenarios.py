import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAMS = os.path.join(ROOT, "params")
CLI = [sys.executable, "-m", "ranker.cli"]


def run_cli(args, expect_rc=0):
    proc = subprocess.run(CLI + args, cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != expect_rc:
        raise AssertionError(f"cli rc={proc.returncode}\n{proc.stderr}\n{proc.stdout}")
    return proc


def base_args(snapshot, report, config="rank_config.json", extra=None):
    config_path = config if os.path.isabs(config) else os.path.join(PARAMS, config)
    args = [
        "--candidates", os.path.join(PARAMS, "candidates.jsonl"),
        "--config", config_path,
        "--groups", os.path.join(PARAMS, "feature_groups.json"),
        "--snapshot-dir", snapshot,
        "--report", report,
        "--no-signal-handlers",
    ]
    if extra:
        args += extra
    return args



def strip_runtime(report):
    import copy
    clone = copy.deepcopy(report)
    for req in clone["requests"]:
        req.pop("cache_events", None)
    return clone


class ScenarioTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ranker-e2e-")

    def path(self, name):
        return os.path.join(self.tmp, name)

    def test_full_run_is_deterministic(self):
        run_cli(base_args(self.path("s1"), self.path("r1.json")))
        run_cli(base_args(self.path("s2"), self.path("r2.json")))
        a = json.load(open(self.path("r1.json")))
        b = json.load(open(self.path("r2.json")))
        self.assertEqual(a["requests"], b["requests"])
        self.assertEqual(a["failures"], b["failures"])

    def test_interrupt_and_resume_matches_uninterrupted(self):
        run_cli(base_args(self.path("full"), self.path("full.json")))
        run_cli(base_args(self.path("resume"), self.path("p1.json"),
                          extra=["--stop-after", "5"]))
        run_cli(base_args(self.path("resume"), self.path("p2.json")))
        full = strip_runtime(json.load(open(self.path("full.json"))))
        resumed = strip_runtime(json.load(open(self.path("p2.json"))))
        self.assertEqual(resumed["requests"], full["requests"])
        self.assertEqual(resumed["failures"], full["failures"])
        self.assertGreater(resumed["cache"]["totals"]["hits"], 0)

    def test_worker_count_does_not_change_results(self):
        reports = []
        for workers in (1, 3, 7):
            cfg = json.load(open(os.path.join(PARAMS, "rank_config.json")))
            cfg["max_workers"] = workers
            cfg_path = self.path(f"cfg{workers}.json")
            json.dump(cfg, open(cfg_path, "w"), indent=2, sort_keys=True)
            run_cli(base_args(self.path(f"snap{workers}"), self.path(f"r{workers}.json"),
                              config=cfg_path))
            reports.append(json.load(open(self.path(f"r{workers}.json"))))
        base = strip_runtime(reports[0])
        for other in reports[1:]:
            other = strip_runtime(other)
            self.assertEqual(other["requests"], base["requests"])
            self.assertEqual(other["failures"], base["failures"])

    def test_config_mismatch_rejected(self):
        run_cli(base_args(self.path("snap"), self.path("r1.json")))
        changed = json.load(open(os.path.join(PARAMS, "rank_config.json")))
        changed["temperature"] = 2.5
        cfg_path = self.path("changed.json")
        json.dump(changed, open(cfg_path, "w"), indent=2, sort_keys=True)
        proc = subprocess.run(
            CLI + base_args(self.path("snap"), self.path("bad.json"),
                            config=cfg_path),
            cwd=ROOT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 2)
        payload = json.load(open(self.path("bad.json")))
        self.assertEqual(payload["code"], "E_CONFIG_MISMATCH")

    def test_incremental_invalidation_on_weight_change(self):
        run_cli(base_args(self.path("snap"), self.path("base.json")))
        changed = json.load(open(os.path.join(PARAMS, "rank_config.json")))
        changed["weights"][0] += 0.1
        cfg_path = self.path("changed.json")
        json.dump(changed, open(cfg_path, "w"), indent=2, sort_keys=True)
        run_cli(base_args(self.path("snap"), self.path("incr.json"),
                          config=cfg_path,
                          extra=["--incremental"]))
        report = json.load(open(self.path("incr.json")))
        totals = report["cache"]["totals"]
        self.assertEqual(totals["hits"], len(report["requests"]))
        self.assertEqual(totals["invalidations"], len(report["requests"]))
        self.assertEqual(totals["recomputes"], 2 * len(report["requests"]))

    def test_injection_isolation_and_reporting(self):
        run_cli(base_args(self.path("snap"), self.path("inj.json"),
                          extra=["--injection", os.path.join(PARAMS, "injection.json")]))
        report = json.load(open(self.path("inj.json")))
        injected_codes = {(f["request_id"], f["code"]) for f in report["failures"]
                          if f.get("injection_ids")}
        self.assertIn(("req-003", "E_INPUT_INVALID"), injected_codes)
        self.assertIn(("req-005", "E_CONFIG_INVALID"), injected_codes)
        self.assertIn(("req-008", "E_CONFIG_INVALID"), injected_codes)
        req002 = next(r for r in report["requests"] if r["request_id"] == "req-002")
        self.assertIn("inj-004", req002["injection_hits"])
        self.assertTrue(any(p["similarity"] == p["threshold"] for p in req002["penalties"]))
        self.assertEqual(len(report["requests"]), 8)

    def test_invalid_config_rejected_with_business_code(self):
        bad = json.load(open(os.path.join(PARAMS, "rank_config.json")))
        bad["weights"] = bad["weights"][:-1]
        cfg_path = self.path("bad.json")
        json.dump(bad, open(cfg_path, "w"), indent=2, sort_keys=True)
        from ranker.config import load_config
        from ranker.errors import ConfigInvalid
        with self.assertRaises(ConfigInvalid) as ctx:
            load_config(cfg_path)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")

    def test_manifest_atomic_replace_and_versions(self):
        run_cli(base_args(self.path("snap"), self.path("r.json"),
                          extra=["--stop-after", "4"]))
        manifest = json.load(open(os.path.join(self.path("snap"), "manifest.json")))
        self.assertEqual(len(manifest["cursor"]), 4)
        self.assertGreaterEqual(manifest["global_version"], 5)
        self.assertTrue(all(v >= 1 for k, v in manifest["stage_versions"].items()
                            if k != "load"))
        self.assertFalse(manifest["completed"])
        run_cli(base_args(self.path("snap"), self.path("r2.json")))
        manifest2 = json.load(open(os.path.join(self.path("snap"), "manifest.json")))
        self.assertTrue(manifest2["completed"])


if __name__ == "__main__":
    unittest.main()
