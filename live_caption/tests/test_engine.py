"""验收测试：分块重排、临时视图一致、定稿收敛及异常码。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from livecaption.config import DisplayPolicy
from livecaption.consistency import check_view_consistency
from livecaption.engine import CaptionEngine
from livecaption.errors import (
    E_LATENCY_BUDGET, E_LINE_SCREEN, E_LINE_WIDTH, E_RESOURCE_LIMIT,
    E_REWRITE_DEPTH, E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY,
    E_VIEW_INCONSISTENT, E_WORD_DUP, E_WORD_MISSING, BusinessError)
from livecaption.events import WordEvent, dump_jsonl, load_jsonl, sort_events
from livecaption.finalize import finalize_once
from livecaption.generator import generate_stream
from livecaption.lines import assemble_lines
from livecaption.rewrite import RewriteLog
from livecaption.scenarios import SCENARIOS
from livecaption.snapshots import SnapshotStore


def run_scenario(name, strict=True):
    events, policy, _ = SCENARIOS[name]()
    engine = CaptionEngine(policy, events[0].stream_id, strict=strict)
    return engine, events, policy


class TestDeterminism(unittest.TestCase):
    def test_generator_byte_identical(self):
        a1, _ = generate_stream("s", 42, 50, disorder_window=6)
        a2, _ = generate_stream("s", 42, 50, disorder_window=6)
        self.assertEqual([e.to_dict() for e in a1], [e.to_dict() for e in a2])

    def test_jsonl_roundtrip_byte_identical(self):
        events, _ = generate_stream("s", 1, 30)
        with tempfile.TemporaryDirectory() as tmp:
            p1, p2 = os.path.join(tmp, "a.jsonl"), os.path.join(tmp, "b.jsonl")
            dump_jsonl(events, p1)
            dump_jsonl(load_jsonl(p1), p2)
            with open(p1, "rb") as f1, open(p2, "rb") as f2:
                self.assertEqual(f1.read(), f2.read())

    def test_engine_run_deterministic(self):
        texts = []
        for _ in range(2):
            engine, events, policy = run_scenario("normal")
            result = engine.run(events)
            texts.append((result.text, result.chunk_count,
                          result.rewrite_total, engine.temp_version))
        self.assertEqual(texts[0], texts[1])

    def test_chunk_reorder_matches_global_sort(self):
        engine, events, _ = run_scenario("normal")
        engine.run(events)
        chunked = engine._ordered_events()
        self.assertEqual([e.word_index for e in chunked],
                         [e.word_index for e in sort_events(events)])


class TestLineAssembly(unittest.TestCase):
    def setUp(self):
        self.policy = DisplayPolicy(max_line_chars=10, max_lines_per_screen=2)

    def mk(self, idx, word, lang="en"):
        return WordEvent("s", idx, idx * 100, idx * 100 + 80, word, 0.9, lang)

    def test_wrap_at_word_boundary(self):
        events = [self.mk(i, w) for i, w in
                  enumerate(["aa", "bb", "cc", "dd"])]
        lines = assemble_lines(events, self.policy, "s", 0, final=True)
        self.assertEqual([ln.text for ln in lines], ["aa bb cc", "dd"])
        for ln in lines:
            self.assertLessEqual(ln.width, self.policy.max_line_chars)

    def test_cjk_width(self):
        events = [self.mk(0, "我们今天", "zh"), self.mk(1, "直播", "zh"),
                  self.mk(2, "开始", "zh")]
        lines = assemble_lines(events, self.policy, "s", 0, final=True)
        self.assertEqual(lines[0].text, "我们今天")
        self.assertEqual(lines[1].text, "直播 开始")

    def test_line_width_error(self):
        events = [self.mk(0, "x" * 40)]
        with self.assertRaises(BusinessError) as ctx:
            assemble_lines(events, self.policy, "s", 3, final=True)
        self.assertEqual(ctx.exception.code, E_LINE_WIDTH)
        self.assertEqual(ctx.exception.details["stream_id"], "s")
        self.assertEqual(ctx.exception.details["chunk_seq"], 3)

    def test_line_screen_error(self):
        engine = CaptionEngine(
            DisplayPolicy(max_line_chars=6, max_lines_per_screen=1,
                          max_wait_ms=10_000), "s", strict=True)
        events = [WordEvent("s", i, i * 300, i * 300 + 200, "ww", 0.9, "en",
                            i, i * 40) for i in range(6)]
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_LINE_SCREEN)
        self.assertIn("chunk_seq", ctx.exception.details)


class TestRewrite(unittest.TestCase):
    def test_late_event_triggers_versioned_rewrite(self):
        engine, events, _ = run_scenario("late_rollback")
        engine.run(events)
        self.assertGreater(engine.rewrite_log.current_version, 0)
        versions = [r.version for r in engine.rewrite_log.records]
        self.assertEqual(versions, sorted(versions))
        for rec in engine.rewrite_log.records:
            self.assertEqual(rec.reason, "late_event")
            self.assertTrue(rec.new_lines)

    def test_rewrite_recompute_consistent(self):
        engine, events, policy = run_scenario("late_rollback")
        engine.run(events)
        for rec in engine.rewrite_log.records:
            wanted = set(rec.word_indices)
            chunk_events = [e for e in events if e.word_index in wanted]
            recomputed = engine.rewrite_log.recompute(rec, chunk_events,
                                                      final=True)
            self.assertEqual([ln.text for ln in rec.new_lines],
                             [ln.text for ln in recomputed])

    def test_rewrite_depth_exceeded(self):
        engine, events, _ = run_scenario("rewrite_depth")
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_REWRITE_DEPTH)

    def test_history_eviction_degraded(self):
        policy = DisplayPolicy(max_wait_ms=80, rollback_steps=50,
                               max_rewrite_history=3)
        events, _, _ = SCENARIOS["late_rollback"]()
        engine = CaptionEngine(policy, events[0].stream_id, strict=False)
        engine.run(events)
        self.assertLessEqual(len(engine.rewrite_log.records), 3)
        if engine.rewrite_log.evicted_count:
            self.assertIn("REWRITE_HISTORY_EVICTED",
                          engine.rewrite_log.degraded_categories)


class TestViewConsistency(unittest.TestCase):
    def test_checkpoints_consistent(self):
        events, policy, _ = SCENARIOS["normal"]()
        engine = CaptionEngine(policy, events[0].stream_id, strict=True)
        for ev in events:  # ingest 内部每个检查点自检，抛错即失败
            engine.ingest(ev)
        engine.finish()

    def test_tampered_view_detected(self):
        events, policy, _ = SCENARIOS["normal"]()
        engine = CaptionEngine(policy, events[0].stream_id, strict=True)
        for ev in events[:20]:
            engine.ingest(ev)
        visible = engine._visible_screen()
        received = list(engine.received)
        tampered = [e for e in received if e.word_index != 7]
        with self.assertRaises(BusinessError) as ctx:
            check_view_consistency(visible, tampered, policy,
                                   events[0].stream_id, "test")
        self.assertEqual(ctx.exception.code, E_VIEW_INCONSISTENT)
        self.assertIn("first_divergent_line", ctx.exception.details)
        self.assertIn("visible_line", ctx.exception.details)
        self.assertIn("recomputed_line", ctx.exception.details)


class TestConvergence(unittest.TestCase):
    def test_final_converges_with_oneshot(self):
        for name in ("normal", "late_rollback"):
            engine, events, policy = run_scenario(name)
            result = engine.run(events)
            oneshot = finalize_once(events, policy, events[0].stream_id)
            self.assertEqual(result.text, oneshot.text)
            self.assertTrue(engine.converged)
            self.assertEqual(result.chunk_count,
                             len(engine.finalized_chunk_seqs))
            self.assertEqual(result.rewrite_total,
                             engine.rewrite_log.current_version)

    def test_word_dup_detected(self):
        engine, events, _ = run_scenario("word_dup")
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_WORD_DUP)

    def test_word_missing_detected(self):
        engine, events, _ = run_scenario("word_missing")
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_WORD_MISSING)


class TestLatency(unittest.TestCase):
    def test_latency_budget_exceeded(self):
        engine, events, _ = run_scenario("latency_breach")
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_LATENCY_BUDGET)
        self.assertIn("completed_chunks", ctx.exception.details)
        self.assertIn("temp_version", ctx.exception.details)

    def test_chunk_plan_records_wait_and_rationale(self):
        engine, events, _ = run_scenario("normal")
        engine.run(events)
        self.assertTrue(engine.latency.plans)
        for plan in engine.latency.plans:
            self.assertLessEqual(plan.chosen_wait_ms,
                                 engine.policy.max_wait_ms)
            self.assertTrue(plan.rationale)
        for timing in engine.latency.timings:
            self.assertGreaterEqual(timing.process_us, 0)
            self.assertGreaterEqual(timing.event_count, 1)


class TestSnapshots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_commit_and_load(self):
        store = SnapshotStore(self.tmp)
        store.commit("policy", 1, '{"a":1}')
        doc = store.load("policy")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["payload"], '{"a":1}')

    def test_whole_replace_on_change(self):
        store = SnapshotStore(self.tmp)
        store.commit("lines", 1, "v1-content")
        store.commit("lines", 2, "v2-content")
        self.assertEqual(store.load("lines")["payload"], "v2-content")
        self.assertEqual(store.meta("lines").version, 2)

    def test_rollback_rejected(self):
        store = SnapshotStore(self.tmp)
        store.commit("lines", 5, "newer")
        with self.assertRaises(BusinessError) as ctx:
            store.commit("lines", 3, "older")
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_ROLLBACK)
        self.assertEqual(store.load("lines")["payload"], "newer")

    def test_corrupt_detected(self):
        store = SnapshotStore(self.tmp)
        store.commit("manifest", 2, "good")
        path = store._path("manifest")
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read().replace("good", "evil")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        with self.assertRaises(BusinessError) as ctx:
            store.load("manifest")
        self.assertEqual(ctx.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(ctx.exception.details["category"], "manifest")
        self.assertEqual(ctx.exception.details["version"], 2)

    def test_stage_busy_nonblocking(self):
        store = SnapshotStore(self.tmp)
        store._commit_lock.acquire()
        try:
            with self.assertRaises(BusinessError) as ctx:
                store.commit("lines", 1, "x")
            self.assertEqual(ctx.exception.code, E_STAGE_BUSY)
        finally:
            store._commit_lock.release()

    def test_concurrent_commits_single_winner_per_instant(self):
        store = SnapshotStore(self.tmp)
        results = []
        lock = threading.Lock()

        def worker(i):
            try:
                store.commit("lines", 100 + i, f"payload-{i}")
                with lock:
                    results.append("ok")
            except BusinessError as err:
                with lock:
                    results.append(err.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 8)
        self.assertTrue(all(r in ("ok", E_STAGE_BUSY) for r in results))


class TestResourceLimits(unittest.TestCase):
    def test_event_limit(self):
        engine, events, _ = run_scenario("resource_limit")
        with self.assertRaises(BusinessError) as ctx:
            engine.run(events)
        self.assertEqual(ctx.exception.code, E_RESOURCE_LIMIT)

    def test_resident_snapshot_eviction(self):
        events, _, _ = SCENARIOS["normal"]()
        policy = DisplayPolicy(max_resident_snapshots=5)
        engine = CaptionEngine(policy, events[0].stream_id, strict=False)
        engine.run(events)
        self.assertLessEqual(len(engine.line_snapshots), 5)
        self.assertIn("RESIDENT_SNAPSHOT_EVICTED", engine.degraded)


class TestScenarios(unittest.TestCase):
    def test_all_scenarios_runnable(self):
        expect_errors = {
            "latency_breach": E_LATENCY_BUDGET,
            "line_width": E_LINE_WIDTH,
            "word_dup": E_WORD_DUP,
            "word_missing": E_WORD_MISSING,
            "rewrite_depth": E_REWRITE_DEPTH,
            "resource_limit": E_RESOURCE_LIMIT,
        }
        for name in SCENARIOS:
            engine, events, _ = run_scenario(name, strict=False)
            engine.run(events)
            codes = [e["code"] for e in engine.errors]
            if name in expect_errors:
                self.assertIn(expect_errors[name], codes, name)
            else:
                self.assertEqual(codes, [], f"{name}: {codes}")
                self.assertTrue(engine.converged, name)


if __name__ == "__main__":
    unittest.main()
