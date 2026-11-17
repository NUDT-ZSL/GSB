import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from platform import manifest as mf
from platform.cli import check_port_free, launch_node, stop_nodes
from platform.distribute import Distributor
from platform.errors import (E_CONVERGE, E_FORMAT, E_PORT, E_REPLAY, E_VERSION,
                             PlatformError, StructuredLogger)
from platform.plan import compile_plan
from platform.snapshot import (load_snapshot, restore_snapshot, save_snapshot)
from platform.store import Store
from platform.upgrade import InjectedKill, upgrade_directory
from platform.verify import verify_db

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLES = os.path.join(ROOT, "samples")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_manifest(strategy="broadcast", derive=None, versions=None):
    versions = versions or {"alpha": 1, "beta": 2, "gamma": 3}
    return mf.normalize({
        "format_version": 3,
        "entries": [{"key": k, "value": f"val-{k}", "version": v,
                     "targets": ["*"]} for k, v in versions.items()],
        "policy": {"strategy": strategy, "max_concurrency": 3,
                   "timeout_ms": 400, "retries": 2},
        "derive": derive or [],
    })


class ManifestTests(unittest.TestCase):
    def test_three_versions_same_digest(self):
        digests = []
        for name in ("manifest_v1.json", "manifest_v2.json", "manifest_v3.json"):
            m = mf.load(os.path.join(SAMPLES, name))
            self.assertEqual(m["format_version"], 3)
            digests.append(mf.canonical_digest(m))
        self.assertEqual(len(set(digests)), 1, "semantic-equal manifests must "
                                               "share one canonical digest")

    def test_field_order_and_whitespace_invariant(self):
        a = mf.loads('{"format_version":1,"entries":{"b":"2","a":"1"}}')
        b = mf.loads('{ "entries": { "a": "1", "b": "2" }, "format_version": 1 }')
        self.assertEqual(mf.canonical_digest(a), mf.canonical_digest(b))

    def test_corrupt_manifest_reports_field_path(self):
        with self.assertRaises(PlatformError) as ctx:
            mf.load(os.path.join(SAMPLES, "corrupt_manifest.json"))
        self.assertEqual(ctx.exception.code, E_FORMAT)
        self.assertEqual(ctx.exception.details["path"], "$.entries[1].version")

    def test_unknown_version_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            mf.loads('{"format_version": 9, "entries": {}}')
        self.assertEqual(ctx.exception.code, E_FORMAT)
        self.assertEqual(ctx.exception.details["path"], "$.format_version")

    def test_missing_policy_v3_rejected(self):
        with self.assertRaises(PlatformError) as ctx:
            mf.loads('{"format_version":3,"entries":[],"derive":[]}')
        self.assertEqual(ctx.exception.details["path"], "$.policy")


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        shutil.copy(os.path.join(SAMPLES, "manifest_v1.json"),
                    os.path.join(self.dir, "a_v1.json"))
        shutil.copy(os.path.join(SAMPLES, "manifest_v2.json"),
                    os.path.join(self.dir, "b_v2.json"))

    def test_upgrade_with_kill_and_resume(self):
        with self.assertRaises(InjectedKill):
            upgrade_directory(self.dir, fail_after=2)
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, ".upgrade_checkpoint.json")))

        report = upgrade_directory(self.dir)
        self.assertTrue(report["resumed"])
        self.assertEqual(sorted(report["upgraded"]), ["a_v1.json", "b_v2.json"])
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, ".upgrade_checkpoint.json")))

        expected = mf.load(os.path.join(SAMPLES, "manifest_v3.json"))
        for name in ("a_v1.json", "b_v2.json"):
            upgraded = mf.load(os.path.join(self.dir, name))
            self.assertEqual(upgraded["format_version"], 3)
            self.assertEqual(mf.canonical_digest(upgraded),
                             mf.canonical_digest(expected))
            backup = os.path.join(self.dir, "backup", name)
            self.assertTrue(os.path.exists(backup))
            mode = stat.S_IMODE(os.stat(backup).st_mode)
            self.assertEqual(mode & stat.S_IWUSR, 0, "backup must be read-only")

        again = upgrade_directory(self.dir)
        self.assertEqual(again["upgraded"], [], "no entry may be reprocessed")


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "platform.db")
        self.log = StructuredLogger(os.path.join(self.tmp, "log.jsonl"))
        self.procs = []
        self.addCleanup(stop_nodes, self.procs)

    def start_nodes(self, node_ids, flaky=None):
        ports = {}
        for n in node_ids:
            port = free_port()
            extra = ["--fail-attempts", "1"] if n == flaky else []
            self.procs.append(launch_node(n, port, extra))
            ports[n] = port
        return ports

    def run_session(self, manifest, nodes, ports):
        store = Store(self.db)
        plan = compile_plan(manifest, nodes)
        version = max(e["version"] for e in manifest["entries"])
        sid = store.create_session(version)
        store.transition(sid, "distributing")
        store.record_plan(sid, plan)
        store.set_meta(f"manifest_v{version}", mf.dumps(manifest))
        return store, plan, sid, version

    def test_parallel_distribution_converges(self):
        nodes = ["node-1", "node-2", "node-3"]
        ports = self.start_nodes(nodes)
        manifest = make_manifest(
            strategy="shard",
            derive=[{"node": "*", "from": ["alpha", "beta"],
                     "algorithm": "sha256"}])
        store, plan, sid, version = self.run_session(manifest, nodes, ports)
        receipts = Distributor(store, self.log).run(sid, plan, ports,
                                                    manifest["policy"])
        for n in nodes:
            self.assertEqual(receipts[n]["derived"],
                             plan[n]["expected_derived"])
            self.assertEqual(receipts[n]["version"], plan[n]["version"])
            self.assertEqual(receipts[n]["seq"], 1)
        self.assertEqual(store.commit(sid)["result"], "committed")
        store.close()
        summary = verify_db(self.db)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["committed_version"], version)

    def test_retry_receipt_distinguished(self):
        nodes = ["node-1", "node-2"]
        ports = self.start_nodes(nodes, flaky="node-2")
        manifest = make_manifest()
        store, plan, sid, _ = self.run_session(manifest, nodes, ports)
        receipts = Distributor(store, self.log).run(sid, plan, ports,
                                                    manifest["policy"])
        self.assertEqual(receipts["node-1"]["attempt"], 1)
        self.assertEqual(receipts["node-2"]["attempt"], 2)
        rows = store.get_receipts(sid)
        self.assertEqual({(r["node"], r["attempt"]) for r in rows},
                         {("node-1", 1), ("node-2", 2)})
        store.close()

    def test_converge_error_lists_deviations_stably(self):
        nodes = ["node-1", "node-2", "node-3"]
        ports = self.start_nodes(["node-1", "node-2"])
        ports["node-3"] = free_port()  # nothing listens: node never receipts
        manifest = make_manifest()
        store, plan, sid, _ = self.run_session(manifest, nodes, ports)
        with self.assertRaises(PlatformError) as ctx:
            Distributor(store, self.log).run(sid, plan, ports,
                                             manifest["policy"])
        err = ctx.exception
        self.assertEqual(err.code, E_CONVERGE)
        deviations = err.details["deviations"]
        self.assertEqual([d["node"] for d in deviations], ["node-3"])
        self.assertIsNone(deviations[0]["actual"])
        self.assertEqual(deviations[0]["expected"],
                         plan["node-3"]["expected_derived"])
        store.fail_session(sid)
        states = [t["to_state"] for t in store.get_transitions(sid)]
        self.assertEqual(states, ["prepared", "distributing", "failed"])
        store.close()


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = Store(os.path.join(self.tmp, "s.db"))
        self.addCleanup(self.store.close)

    def test_idempotent_commit(self):
        sid = self.store.create_session(1)
        self.store.transition(sid, "distributing")
        first = self.store.commit(sid)
        second = self.store.commit(sid)
        self.assertEqual(first["result"], "committed")
        self.assertEqual(second["result"], "already_committed")
        self.assertEqual(first["version"], second["version"])

    def test_uncommitted_version_cannot_be_skipped(self):
        sid1 = self.store.create_session(1)
        self.store.transition(sid1, "distributing")
        sid2 = self.store.create_session(2)
        self.store.transition(sid2, "distributing")
        with self.assertRaises(PlatformError) as ctx:
            self.store.commit(sid2)
        self.assertEqual(ctx.exception.code, E_VERSION)
        self.assertEqual(ctx.exception.details["blocking"], [1])
        self.assertEqual(self.store.commit(sid1)["result"], "committed")
        self.assertEqual(self.store.commit(sid2)["result"], "committed")

    def test_failed_session_retry_keeps_trail(self):
        sid = self.store.create_session(1)
        self.store.transition(sid, "distributing")
        self.store.fail_session(sid)
        self.store.transition(sid, "distributing")  # retry
        self.store.commit(sid)
        trail = [(t["from_state"], t["to_state"])
                 for t in self.store.get_transitions(sid)]
        self.assertEqual(trail, [(None, "prepared"), ("prepared", "distributing"),
                                 ("distributing", "failed"),
                                 ("failed", "distributing"),
                                 ("distributing", "committed")])
        for t in self.store.get_transitions(sid):
            self.assertIn("version", t)
            self.assertIn("confirmed_nodes", t)
            self.assertIn("ts", t)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "s.db")
        self.manifest = make_manifest()

    def _committed_store(self, version=3):
        store = Store(self.db)
        sid = store.create_session(version)
        store.transition(sid, "distributing")
        plan = compile_plan(self.manifest, ["node-1"])
        store.record_plan(sid, plan)
        store.record_receipt(sid, "node-1", 1, version, 1,
                             plan["node-1"]["expected_derived"])
        store.set_meta(f"manifest_v{version}", mf.dumps(self.manifest))
        store.commit(sid)
        return store, plan

    def test_snapshot_roundtrip_and_wholesale_replace(self):
        store, plan = self._committed_store(version=3)
        snap_path = os.path.join(self.tmp, "snap.json")
        save_snapshot(store, self.manifest, snap_path)
        store.close()

        # mutate current state: commit a newer version
        store = Store(self.db)
        sid = store.create_session(4)
        store.transition(sid, "distributing")
        store.commit(sid)
        store.close()

        snap = load_snapshot(snap_path)
        snap["committed_version"] = 3  # keep conflict check out of scope here
        store = Store(self.db)
        result = restore_snapshot(store, snap, current_manifest_version=2)
        self.assertTrue(result["replaced"], "manifest version mismatch must "
                                            "trigger wholesale replacement")
        self.assertEqual(store.last_committed()["version"], 3)
        restored_plan = store.get_plan(1)
        self.assertEqual(restored_plan["node-1"]["expected_derived"],
                         plan["node-1"]["expected_derived"])
        store.close()

    def test_snapshot_version_conflict_e_version(self):
        store, _ = self._committed_store(version=3)
        store.close()
        snap = load_snapshot(os.path.join(SAMPLES, "conflict_snapshot.json"))
        store = Store(self.db)
        with self.assertRaises(PlatformError) as ctx:
            # same manifest version as the snapshot -> committed-version
            # conflict (downgrade) instead of a wholesale replacement
            restore_snapshot(store, snap, current_manifest_version=2)
        self.assertEqual(ctx.exception.code, E_VERSION)
        store.close()
        # session DB must reopen and remain usable after the injected fault
        store = Store(self.db)
        self.assertEqual(store.last_committed()["version"], 3)
        store.close()


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = os.path.join(self.tmp, "v.db")
        self.manifest = make_manifest()
        store = Store(self.db)
        plan = compile_plan(self.manifest, ["node-1", "node-2"])
        version = max(e["version"] for e in self.manifest["entries"])
        sid = store.create_session(version)
        store.transition(sid, "distributing")
        store.record_plan(sid, plan)
        for n in ("node-1", "node-2"):
            store.record_receipt(sid, n, 1, version, 1,
                                 plan[n]["expected_derived"])
        store.set_meta(f"manifest_v{version}", mf.dumps(self.manifest))
        store.commit(sid)
        store.close()
        self.version = version

    def test_verify_ok(self):
        summary = verify_db(self.db)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(sorted(summary["nodes"]), ["node-1", "node-2"])

    def test_replay_missing_manifest(self):
        store = Store(self.db)
        with store.conn:
            store.conn.execute("DELETE FROM meta WHERE key=?",
                               (f"manifest_v{self.version}",))
        store.close()
        with self.assertRaises(PlatformError) as ctx:
            verify_db(self.db)
        self.assertEqual(ctx.exception.code, E_REPLAY)
        self.assertIn("missing", ctx.exception.message)

    def test_replay_node_downgrade(self):
        store = Store(self.db)
        store.record_receipt(1, "node-1", 2, self.version - 1, 2, "0" * 64)
        store.close()
        with self.assertRaises(PlatformError) as ctx:
            verify_db(self.db)
        self.assertEqual(ctx.exception.code, E_REPLAY)
        self.assertEqual(ctx.exception.details["nodes"], ["node-1"])


class InjectionTests(unittest.TestCase):
    def test_port_busy_e_port(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        try:
            with self.assertRaises(PlatformError) as ctx:
                check_port_free(port)
            self.assertEqual(ctx.exception.code, E_PORT)
        finally:
            blocker.close()

    def test_inject_corrupt_manifest_cli_writes_structured_log(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        log = os.path.join(tmp, "log.jsonl")
        proc = subprocess.run(
            [sys.executable, "-m", "platform.cli", "--log", log,
             "inject", "corrupt-manifest", "--file",
             os.path.join(SAMPLES, "corrupt_manifest.json")],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"], E_FORMAT)
        with open(log) as fh:
            events = [json.loads(line) for line in fh]
        self.assertEqual(events[-1]["error"], E_FORMAT)
        self.assertEqual(events[-1]["event"], "inject_corrupt-manifest")


if __name__ == "__main__":
    unittest.main()
