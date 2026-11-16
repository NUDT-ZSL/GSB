"""端到端引擎：并行/串行一致、快照恢复、配置不匹配、信号。"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from dp_bench.engine import ParseEngine, ShutdownController
from dp_bench.snapshot import ConfigMismatchError
from dp_bench.loader import load_config, load_model, load_samples
from dp_bench.model import sha256_hex
from dp_bench.snapshot import SnapshotStore

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dpbench-"))
        self.model = load_model(DATA / "model.json")
        self.sentences = load_samples(DATA / "sentences.txt")
        self.config = load_config(DATA / "config.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _engine(self, config=None, store=None, shutdown=None):
        config = config or self.config
        return ParseEngine(self.model, config, self.sentences,
                           snapshot_store=store, shutdown=shutdown)

    def test_parallel_serial_field_identical(self):
        serial_cfg = dict(self.config, max_workers=1, batch_size=1)
        serial = self._engine(serial_cfg).run()
        parallel = self._engine(dict(self.config)).run()
        self.assertEqual(
            serial["consistency"]["determinism_digest"],
            parallel["consistency"]["determinism_digest"])
        self.assertEqual(serial["sentences"], parallel["sentences"])
        self.assertEqual(serial["failures"], parallel["failures"])
        self.assertEqual(serial["budget"]["deduction_sequence"],
                         parallel["budget"]["deduction_sequence"])
        self.assertEqual(serial["budget"]["consumption_curve"],
                         parallel["budget"]["consumption_curve"])
        self.assertGreaterEqual(parallel["observability"]["thread_peak"], 1)

    def test_snapshot_resume_field_identical(self):
        baseline = self._engine().run()

        # 模拟中断：仅前 5 句落盘快照
        store_dir = self.tmp / "snap"
        store = SnapshotStore(store_dir, sha256_hex(self.config),
                              self.model.content_hash())
        engine = self._engine(dict(self.config), store=store)
        partial_ids = self._run_partial(engine, 5)
        self.assertEqual(len(partial_ids), 5)

        # 新引擎依据快照恢复并跑完全部句子
        store2 = SnapshotStore(store_dir, sha256_hex(self.config),
                               self.model.content_hash())
        snap = store2.resume_state()
        self.assertIsNotNone(snap)
        engine2 = self._engine(dict(self.config), store=store2)
        pre = {sid: store2.load_result(sid)["steps_used"]
               for sid in snap["completed_sentence_ids"]}
        from dp_bench.budget import BudgetLedger
        engine2.ledger = BudgetLedger(
            engine2.sentence_ids, self.config["global_step_budget"],
            self.config["per_sentence_quota"], pre_consumed=pre)
        for sid in snap["completed_sentence_ids"]:
            engine2.completed[sid] = store2.load_result(sid)
        resumed = engine2.run()

        self.assertEqual(
            resumed["consistency"]["determinism_digest"],
            baseline["consistency"]["determinism_digest"])
        self.assertEqual(resumed["sentences"], baseline["sentences"])
        self.assertEqual(resumed["budget"], baseline["budget"])

    def _run_partial(self, engine, n):
        import concurrent.futures
        index = dict(self.sentences)
        ids = engine.sentence_ids[:n]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(engine._run_one, sid, index[sid])
                    for sid in ids]
            for fut in futs:
                fut.result()
        return ids

    def test_config_mismatch_refuses_resume(self):
        store_dir = self.tmp / "snap"
        store = SnapshotStore(store_dir, sha256_hex(self.config),
                              self.model.content_hash())
        store.write_snapshot("parse", ["S01"], 9, 1)
        wrong_store = SnapshotStore(store_dir, "deadbeef",
                                    self.model.content_hash())
        with self.assertRaises(ConfigMismatchError):
            wrong_store.resume_state()

    def test_atomic_snapshot_files_complete(self):
        store_dir = self.tmp / "snap"
        store = SnapshotStore(store_dir, sha256_hex(self.config),
                              self.model.content_hash())
        engine = self._engine(dict(self.config), store=store)
        engine.run()
        latest = json.loads((store_dir / "latest.json").read_text(
            encoding="utf-8"))
        self.assertEqual(latest["stage"], "summary")
        self.assertEqual(len(latest["completed_sentence_ids"]),
                         len(self.sentences))
        versions = store.list_version_files()
        self.assertGreaterEqual(len(versions), len(self.sentences))
        # 不存在残留临时文件
        leftovers = list(store_dir.rglob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_shutdown_event_stops_dispatch(self):
        shutdown = ShutdownController()
        shutdown.event.set()
        shutdown.signal_name = "SIGINT"
        engine = self._engine(dict(self.config), shutdown=shutdown)
        report = engine.run()
        # 信号在派发前置位：没有句子被调度，但快照仍落盘
        self.assertEqual(report["sentences"], [])
        self.assertEqual(shutdown.signal_name, "SIGINT")

    def test_shutdown_mid_run_completes_inflight_and_persists(self):
        store_dir = self.tmp / "snapmid"
        store = SnapshotStore(store_dir, sha256_hex(self.config),
                              self.model.content_hash())
        shutdown = ShutdownController()
        engine = self._engine(dict(self.config, batch_size=2),
                              store=store, shutdown=shutdown)

        import threading
        def stop_after_first():
            # 等待第一句落盘后请求退出，验证在途句完成、后续句不再派发
            while not engine.completed:
                pass
            shutdown.event.set()
            shutdown.signal_name = "SIGTERM"
        watcher = threading.Thread(target=stop_after_first)
        watcher.start()
        report = engine.run()
        watcher.join(timeout=5)

        self.assertEqual(shutdown.signal_name, "SIGTERM")
        completed = len(report["sentences"])
        self.assertGreaterEqual(completed, 1)
        self.assertLess(completed, len(self.sentences))
        # 已完成句均已原子落盘，快照记录数量一致
        snap = store.latest()
        self.assertEqual(len(snap["completed_sentence_ids"]), completed)
        self.assertTrue((store_dir / "latest.json").exists())


if __name__ == "__main__":
    unittest.main()
