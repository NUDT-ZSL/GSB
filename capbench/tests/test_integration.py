import json
import os
import shutil
import tempfile
import threading
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from capbench.clock import MonotonicClock
from capbench.driver import request_stop_after
from capbench.errors import ClockError, PortError, SceneError, ReplayError
from capbench.mock_server import MockServer, ServerConfig, read_server_log
from capbench.replay import replay_snapshot
from capbench.scene import load_scene
from capbench.session import SessionRunner
from capbench.snapshot import SnapshotStore
from capbench.versioning import VersionStore

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class IntegrationBase(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.mkdtemp(prefix="capbench-it-")
        self.store = SnapshotStore(os.path.join(self.workspace, "snapshots"))
        self.versions = VersionStore(
            os.path.join(self.workspace, "state", "versions.json"))
        self.server_log = os.path.join(self.workspace, "server.log.jsonl")
        self.cfg = ServerConfig(port=8451, process_ms=20,
                                error_rate_bp=100, seed=20261117,
                                log_path=self.server_log)

    def tearDown(self):
        shutil.rmtree(self.workspace, ignore_errors=True)

    def runner(self):
        return SessionRunner(
            self.store, self.versions,
            server_config={
                "host": self.cfg.host, "port": self.cfg.port,
                "process_ms": self.cfg.process_ms,
                "error_rate_bp": self.cfg.error_rate_bp,
                "seed": self.cfg.seed})

    def scene(self, name="cold_start.json", port=8451):
        path = os.path.join(HERE, "scenarios", name)
        scene = load_scene(path)
        scene.raw["target"]["port"] = port
        from capbench.models import TargetSpec
        object.__setattr__(scene, "target", TargetSpec("127.0.0.1", port))
        return scene


class FullLadderTest(IntegrationBase):
    def test_full_ladder_snapshot_and_server_truth(self):
        scene = self.scene()
        with MockServer(self.cfg):
            report = self.runner().run("it-cold", scene)
        self.assertEqual(len(report.completed), 3)
        self.assertFalse(report.interrupted)
        snapshot = self.store.load("it-cold")
        self.assertEqual(snapshot.status, "completed")
        # truth source: server log counts match snapshot counters
        log_rows = read_server_log(self.server_log)
        logged_ok = sum(1 for r in log_rows if r["status"] == "ok")
        logged_err = sum(1 for r in log_rows if r["status"] == "error")
        snap_ok = sum(r["metrics"]["total_requests"] for r in snapshot.stage_results)
        self.assertEqual(logged_ok + logged_err, snap_ok)
        for result in snapshot.stage_results:
            self.assertEqual(result["observation"]["clock_basis"],
                             "monotonic_ns->integer_ms")
            self.assertIn("receipt", result["observation"]["truth_source"])
            self.assertTrue(result["observation"]["samples"])


class StaleReplacementTest(IntegrationBase):
    def test_rerun_wholly_replaces_stale_observations(self):
        scene1 = self.scene()
        scene2 = self.scene("rerun_concurrency.json")
        with MockServer(self.cfg):
            first = self.runner().run("it-rerun", scene1)
            self.assertEqual(first.params_version, 1)
            second = self.runner().run("it-rerun", scene2)
        self.assertEqual(second.params_version, 2)
        rep = second.replacement
        self.assertTrue(rep.fully_replaced)
        self.assertEqual(rep.replaced_stage_indexes, [1, 2, 3])
        self.assertEqual(rep.old_version, 1)
        snapshot = self.store.load("it-rerun")
        self.assertEqual(len(snapshot.stage_results), 3)
        for result in snapshot.stage_results:
            self.assertEqual(result["params_version"], 2)
            self.assertEqual(result["spec"]["concurrency"],
                             scene2.stages[result["stage_index"] - 1].concurrency)

    def test_unchanged_scene_keeps_version_and_is_not_stale(self):
        scene = self.scene()
        with MockServer(self.cfg):
            self.runner().run("it-same", scene)
            again = self.runner().run("it-same", scene)
        self.assertFalse(again.replacement.fully_replaced)
        self.assertEqual(again.replacement.old_version, None)


class ForcedTerminationTest(IntegrationBase):
    def test_interrupted_stage_unknown_and_recovery_plan(self):
        scene = self.scene()
        stop = threading.Event()
        request_stop_after(stop, delay_s=3.4)
        with MockServer(self.cfg):
            report = self.runner().run("it-kill", scene, stop_event=stop)
        self.assertTrue(report.interrupted)
        snapshot = self.store.load("it-kill")
        self.assertEqual(snapshot.status, "interrupted")
        plan = SessionRunner.recovery_plan(snapshot)
        self.assertIn(2, plan["unknown_stage_indexes"])
        # no fabricated report for unknown stages
        self.assertEqual(plan["finished_stage_indexes"], [1])
        unknown_results = [r for r in snapshot.stage_results
                           if r["stage_index"] in plan["unknown_stage_indexes"]]
        self.assertEqual(unknown_results, [])


class ReplayTest(IntegrationBase):
    def test_replay_matches_snapshot_within_tolerance(self):
        scene = self.scene()
        with MockServer(self.cfg):
            self.runner().run("it-replay", scene)
        snapshot = self.store.load("it-replay")
        with MockServer(self.cfg):
            report = replay_snapshot(snapshot, {
                "host": self.cfg.host, "port": self.cfg.port,
                "process_ms": self.cfg.process_ms,
                "error_rate_bp": self.cfg.error_rate_bp,
                "seed": self.cfg.seed})
        self.assertTrue(report.reproducible)
        self.assertEqual(len(report.items), 3)
        self.assertTrue(all(i.matched for i in report.items))

    def test_replay_config_mismatch_raises_e_replay(self):
        scene = self.scene()
        with MockServer(self.cfg):
            self.runner().run("it-replay2", scene)
        snapshot = self.store.load("it-replay2")
        wrong = dict(snapshot.server_config)
        wrong["process_ms"] = 99
        with MockServer(ServerConfig(
                port=self.cfg.port, process_ms=99,
                error_rate_bp=self.cfg.error_rate_bp, seed=self.cfg.seed,
                log_path=self.server_log)):
            with self.assertRaises(ReplayError) as ctx:
                replay_snapshot(snapshot, wrong)
        self.assertEqual(ctx.exception.code, "E_REPLAY")


class FaultInjectionTest(IntegrationBase):
    def test_port_busy_raises_e_port(self):
        import socket
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", self.cfg.port))
        holder.listen(1)
        try:
            with self.assertRaises(PortError) as ctx:
                MockServer(self.cfg).start()
            self.assertEqual(ctx.exception.code, "E_PORT")
        finally:
            holder.close()

    def test_clock_rollback_raises_e_clock(self):
        clock = MonotonicClock(rollback_ms=1000, rollback_at_tick=2)
        clock.now_ms()
        with self.assertRaises(ClockError) as ctx:
            clock.now_ms()
        self.assertEqual(ctx.exception.code, "E_CLOCK")
        self.assertIn("delta_ms", ctx.exception.details)

    def test_invalid_scene_file_raises_e_scene(self):
        bad = os.path.join(self.workspace, "bad.json")
        with open(bad, "w") as fh:
            fh.write('{"stages": [,,]}')
        with self.assertRaises(SceneError) as ctx:
            load_scene(bad)
        self.assertEqual(ctx.exception.code, "E_SCENE")


if __name__ == "__main__":
    unittest.main()
