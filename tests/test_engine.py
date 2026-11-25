"""End-to-end acceptance tests for the feature-graph engine.

Run with:  python -m unittest discover -s tests -v
Covers: artifact validation, plan determinism, scheduling constraints,
fixed-point error ledgers, ground-truth consistency, graph versioning,
crash/resume bit-identity, and multi-process concurrency.
"""
import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fengine import loader, runner, versioning, verify as verify_mod
from fengine.errors import BizError
from fengine.planner import build_plan
from fengine.store import Store

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SAMPLES = os.path.join(ROOT, "samples")


def _load_json(name):
    with open(os.path.join(SAMPLES, name)) as fh:
        return json.load(fh)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fengine_test_")
        self.db = os.path.join(self.tmp, "t.db")
        self.graph = _load_json("graph.json")
        self.events = _load_json("events.json")
        self.config = _load_json("config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def loaded_store(self, config=None):
        store = Store(self.db)
        cfg = config or self.config
        plan = build_plan(self.graph, self.events, cfg)
        store.load_artifacts(self.graph, self.events, cfg, plan["plan_summary"])
        return store

    def all_units(self, db_path, version=1):
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT node_id, window_id, outputs_json, ledger_json FROM op_units"
            " WHERE graph_version=? ORDER BY node_id, window_id",
            (version,)).fetchall()
        conn.close()
        return rows


class TestValidation(Base):
    def test_valid_artifacts_load(self):
        g, e, c = loader.load_artifacts(
            os.path.join(SAMPLES, "graph.json"),
            os.path.join(SAMPLES, "events.json"),
            os.path.join(SAMPLES, "config.json"))
        self.assertEqual(len(g["nodes"]), 8)
        self.assertEqual(len(e), 120)

    def _expect(self, code, graph=None, events=None, config=None):
        with self.assertRaises(BizError) as ctx:
            loader.validate_graph(graph or self.graph)
            loader.validate_events(events or self.events)
            loader.validate_config(config or self.config)
        self.assertEqual(ctx.exception.code, code)
        self.assertTrue(ctx.exception.details)

    def test_duplicate_node(self):
        g = copy.deepcopy(self.graph)
        g["nodes"].append(copy.deepcopy(g["nodes"][0]))
        self._expect("E_GRAPH_DUPLICATE_NODE", graph=g)

    def test_unknown_dep(self):
        g = copy.deepcopy(self.graph)
        g["nodes"][0]["deps"] = ["ghost"]
        self._expect("E_GRAPH_UNKNOWN_DEP", graph=g)

    def test_cycle(self):
        g = copy.deepcopy(self.graph)
        g["nodes"][3]["deps"] = ["n_scale_win"]
        g["nodes"][5]["deps"] = ["n_win_sum"]
        self._expect("E_GRAPH_CYCLE", graph=g)

    def test_bad_op_type(self):
        g = copy.deepcopy(self.graph)
        g["nodes"][0]["op"] = "magic"
        self._expect("E_OP_TYPE_INVALID", graph=g)

    def test_param_mismatch(self):
        g = copy.deepcopy(self.graph)
        g["nodes"][0]["params"] = {"column": "amount"}
        self._expect("E_OP_PARAM_MISMATCH", graph=g)

    def test_output_type_mismatch(self):
        g = copy.deepcopy(self.graph)
        g["nodes"][0]["output_type"] = "scalar"
        self._expect("E_OP_OUTPUT_TYPE_MISMATCH", graph=g)

    def test_bad_config(self):
        c = dict(self.config, parallel_slots=0)
        self._expect("E_CONFIG_INVALID", config=c)
        c = dict(self.config, tolerance=-0.1)
        self._expect("E_CONFIG_INVALID", config=c)

    def test_event_seq_order(self):
        e = copy.deepcopy(self.events)
        e[5]["seq"] = 4
        self._expect("E_EVENT_SEQ_ORDER", events=e)

    def test_event_digest(self):
        e = copy.deepcopy(self.events)
        e[7]["features"]["amount"] += 1.0
        self._expect("E_EVENT_DIGEST_MISMATCH", events=e)


class TestPlan(Base):
    def test_plan_deterministic(self):
        p1 = build_plan(self.graph, self.events, self.config)
        p2 = build_plan(self.graph, self.events, self.config)
        self.assertEqual(p1["plan_summary"], p2["plan_summary"])
        self.assertEqual(p1["layers"], p2["layers"])

    def test_unique_window_assignment(self):
        plan = build_plan(self.graph, self.events, self.config)
        assigned = [s for seqs in plan["window_seqs"].values() for s in seqs]
        self.assertEqual(sorted(assigned), [e["seq"] for e in self.events])

    def test_layers_topological(self):
        plan = build_plan(self.graph, self.events, self.config)
        order = {nid: i for i, layer in enumerate(plan["layers"]) for nid in layer}
        for node in self.graph["nodes"]:
            for dep in node["deps"]:
                self.assertLess(order[dep], order[node["id"]])


class TestExecution(Base):
    def test_run_and_verify(self):
        store = self.loaded_store()
        stats = runner.run(store)
        self.assertEqual(stats["abandoned"], 0)
        report = verify_mod.verify(store)
        store.close()
        self.assertEqual(report["status"], "ok")
        self.assertLessEqual(report["max_abs_error"], self.config["tolerance"])

    def test_parallel_equals_serial(self):
        store = self.loaded_store()
        runner.run(store, slots=1)
        store.close()
        serial = self.all_units(self.db)
        db2 = self.db + ".p"
        store = Store(db2)
        plan = build_plan(self.graph, self.events, self.config)
        store.load_artifacts(self.graph, self.events, self.config,
                             plan["plan_summary"])
        runner.run(store, slots=4)
        store.close()
        self.assertEqual(serial, self.all_units(db2))

    def test_memory_budget_abandon(self):
        cfg = dict(self.config, memory_budget_bytes=3000)
        store = self.loaded_store(config=cfg)
        stats = runner.run(store)
        self.assertEqual(stats["committed"], 0)
        self.assertGreater(stats["abandoned"], 0)
        rows = store.conn.execute(
            "SELECT DISTINCT abandon_reason FROM op_units"
            " WHERE status='abandoned'").fetchall()
        store.close()
        self.assertEqual([r[0] for r in rows], ["E_MEMORY_BUDGET_EXHAUSTED"])

    def test_error_ledger_traceable(self):
        store = self.loaded_store()
        runner.run(store)
        row = store.conn.execute(
            "SELECT ledger_json FROM op_units WHERE node_id='n_agg_amt_mean'"
            " AND window_id=5 AND graph_version=1").fetchone()
        ledger = json.loads(row["ledger_json"])
        store.close()
        self.assertEqual(ledger["scale"], self.config["fixed_scale"])
        self.assertEqual(ledger["rounding"], "half_even")
        self.assertGreater(len(ledger["entries"]), 0)
        entry = ledger["entries"][0]
        self.assertIn("event_seq", entry)
        self.assertIn("raw_value", entry)
        self.assertIn("quant_error", entry)
        self.assertGreaterEqual(ledger["accumulated_error_bound"], 0.0)


class TestCrashResume(Base):
    def _run_cli(self, db, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        return subprocess.run(
            [sys.executable, "-m", "fengine.cli", "--db", db, *args],
            cwd=ROOT, env=env, capture_output=True, text=True)

    def test_resume_bit_identical(self):
        one_shot = os.path.join(self.tmp, "one.db")
        crashed = os.path.join(self.tmp, "crash.db")
        for db in (one_shot, crashed):
            r = self._run_cli(db, "load", "--graph", f"{SAMPLES}/graph.json",
                              "--events", f"{SAMPLES}/events.json",
                              "--config", f"{SAMPLES}/config.json")
            self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run_cli(one_shot, "run")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self._run_cli(crashed, "run", "--crash-after", "41")
        self.assertEqual(r.returncode, 3)
        conn = sqlite3.connect(crashed)
        half = conn.execute(
            "SELECT COUNT(*) FROM op_units WHERE status='running'").fetchone()[0]
        conn.close()
        self.assertGreater(half, 0)
        r = self._run_cli(crashed, "resume")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.all_units(one_shot), self.all_units(crashed))


class TestVersioning(Base):
    def test_patch_closure_and_reuse(self):
        store = self.loaded_store()
        runner.run(store)
        new_graph = _load_json("graph_v2.json")
        report = versioning.apply_patch(store, new_graph)
        self.assertEqual(report["changed_nodes"], ["n_win_sum"])
        self.assertEqual(set(report["invalidation_closure"]),
                         {"n_win_sum", "n_scale_win",
                          "n_agg_win_mean", "n_agg_s2_max"})
        self.assertEqual(report["reused_units"], 96)
        stats = runner.run(store)
        self.assertEqual(stats["reused"], 96)
        result = verify_mod.verify(store)
        store.close()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["graph_version"], 2)

    def test_rollback_rejected(self):
        store = self.loaded_store()
        runner.run(store)
        versioning.apply_patch(store, _load_json("graph_v2.json"))
        with self.assertRaises(BizError) as ctx:
            versioning.apply_patch(store, self.graph)
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")
        with self.assertRaises(BizError) as ctx:
            versioning.apply_patch(store, _load_json("graph_v2.json"),
                                   target_version=1)
        self.assertEqual(ctx.exception.code, "E_VERSION_ROLLBACK")
        store.close()


class TestConcurrency(Base):
    def test_two_workers_disjoint_windows(self):
        store = self.loaded_store()
        store.close()
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        procs = [
            subprocess.Popen(
                [sys.executable, "-m", "fengine.cli", "--db", self.db,
                 "run", "--worker-id", f"w{i}", "--windows", rng],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL)
            for i, rng in enumerate(("5:17", "17:29"))]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        conn = sqlite3.connect(self.db)
        dup = conn.execute(
            "SELECT COUNT(*) FROM (SELECT node_id, window_id FROM op_units"
            " WHERE graph_version=1 GROUP BY node_id, window_id"
            " HAVING SUM(attempt) > 1)").fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM op_units WHERE status='done'").fetchone()[0]
        maxbal = conn.execute(
            "SELECT MAX(balance_after) FROM memory_ledger").fetchone()[0]
        commits = conn.execute(
            "SELECT COUNT(*) FROM window_commits").fetchone()[0]
        conn.close()
        self.assertEqual(dup, 0)
        self.assertEqual(done, 192)
        self.assertLessEqual(maxbal, self.config["memory_budget_bytes"])
        self.assertEqual(commits, 24)
        store = Store(self.db)
        report = verify_mod.verify(store)
        stats = runner.run(store)
        store.close()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(stats["committed"], 0)


if __name__ == "__main__":
    unittest.main()
