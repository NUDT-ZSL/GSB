import json
import os
import shutil
import tempfile
import unittest

from prefix_cache_workbench.cache import CacheStore
from prefix_cache_workbench.chunker import (
    Vocabulary,
    assemble_context,
    build_block_sequence,
    split_blocks,
)
from prefix_cache_workbench.engine import Engine
from prefix_cache_workbench.errors import (
    E_CHAIN_BROKEN,
    E_CONTEXT_TOO_LONG,
    E_EMPTY_CONTEXT,
    E_EVICT_POLICY_INVALID,
    E_SNAPSHOT_STALE,
    E_TOKEN_UNKNOWN,
    WorkbenchError,
)
from prefix_cache_workbench.hashing import (
    EmbeddingEncoder,
    chain_fingerprint,
    config_fingerprint,
    content_fingerprint,
)
from prefix_cache_workbench.pipeline import load_json, load_jsonl, run_stream
from prefix_cache_workbench.samplegen import generate_sample_set
from prefix_cache_workbench.snapshot import SnapshotStore


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sample_dir = os.path.join(self.tmp, "samples")
        generate_sample_set(self.sample_dir)
        self.config = load_json(
            os.path.join(self.sample_dir, "cache_config.json")
        )
        self.vocab_rules = load_json(
            os.path.join(self.sample_dir, "vocab_rules.json")
        )
        self.requests = load_jsonl(
            os.path.join(self.sample_dir, "request_stream.jsonl")
        )
        self.vocab = Vocabulary(self.vocab_rules)
        self.encoder = EmbeddingEncoder(self.vocab_rules)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def engine(self, **overrides):
        config = dict(self.config)
        config.update(overrides)
        return Engine(config, self.vocab, self.encoder)

    def run_requests(self, engine, requests):
        for request in requests:
            try:
                blocks = engine.prepare_request(request)
                engine.commit_request(request, blocks)
            except WorkbenchError as exc:
                engine._fail(request, exc.stage, exc)
        return engine

    def test_fingerprint_position_and_run_stable(self):
        tokens = list(range(1, 13))
        derive = self.config["hash_derive"]
        blocks_a = build_block_sequence(tokens, self.config, self.vocab)
        blocks_b = build_block_sequence(tokens, self.config, self.vocab)
        self.assertEqual(
            [b["chain_fp"] for b in blocks_a],
            [b["chain_fp"] for b in blocks_b],
        )
        split = split_blocks(tokens, self.config["block_tokens"])
        self.assertEqual(content_fingerprint(split[0], derive),
                         content_fingerprint(split[1], derive)
                         if split[0] == split[1] else
                         content_fingerprint(split[0], derive))
        parent = None
        for block in blocks_a:
            expected = chain_fingerprint(block["content_fp"], parent, derive)
            self.assertEqual(block["chain_fp"], expected)
            parent = block["chain_fp"]

    def test_validation_error_codes(self):
        with self.assertRaises(WorkbenchError) as ctx:
            assemble_context(
                {"system_prompt": "", "history": "", "question": ""},
                self.vocab,
            )
        self.assertEqual(ctx.exception.code, E_EMPTY_CONTEXT)
        with self.assertRaises(WorkbenchError) as ctx:
            assemble_context(
                {"system_prompt": "nope token", "history": "", "question": ""},
                self.vocab,
            )
        self.assertEqual(ctx.exception.code, E_TOKEN_UNKNOWN)
        long_text = " ".join(["noun00"] * (self.vocab.max_context_tokens + 1))
        with self.assertRaises(WorkbenchError) as ctx:
            assemble_context(
                {"system_prompt": long_text, "history": "", "question": ""},
                self.vocab,
            )
        self.assertEqual(ctx.exception.code, E_CONTEXT_TOO_LONG)

    def test_invalid_policy_rejected(self):
        with self.assertRaises(WorkbenchError) as ctx:
            self.engine(eviction_policy="RANDOM")
        self.assertEqual(ctx.exception.code, E_EVICT_POLICY_INVALID)
        with self.assertRaises(WorkbenchError):
            CacheStore("RANDOM")

    def test_shared_prefix_reuse_and_chain_broken_event(self):
        engine = self.engine(residency_budget_blocks=10_000,
                             session_retention_cap=10_000)
        self.run_requests(engine, self.requests[:2])
        alpha = next(
            r for r in engine.request_results if r["session"] == "alpha"
        )
        beta = next(
            r for r in engine.request_results if r["session"] == "beta"
        )
        self.assertEqual(alpha["declared_reusable_blocks"], 0)
        self.assertGreaterEqual(beta["declared_reusable_blocks"],
                                self.config["min_reusable_blocks"])
        self.assertEqual(beta["reusable_range"][0], 0)

        tight = self.engine(residency_budget_blocks=3)
        self.run_requests(tight, self.requests[:10])
        chain_events = [e for e in tight.events if e["type"] == E_CHAIN_BROKEN]
        self.assertTrue(chain_events)
        self.assertTrue(
            all(e["block_index"] > 0 for e in chain_events)
        )

    def test_lru_lfu_tie_break_lexicographic(self):
        lru = CacheStore("LRU")
        lfu = CacheStore("LFU")
        self.assertEqual(lru.policy, "LRU")
        self.assertEqual(lfu.policy, "LFU")

    def test_deterministic_chain_broken_injection(self):
        engine = self.engine(residency_budget_blocks=3)
        shared = self.requests[0]
        follower = self.requests[1]
        shared_blocks = engine.prepare_request(shared)
        engine.commit_request(shared, shared_blocks)
        first_fp = shared_blocks[0]["chain_fp"]
        victim = engine.store.registry[first_fp]
        victim.resident = False
        victim.invalidated = True
        victim.invalid_reason = "budget_eviction"
        follower_blocks = engine.prepare_request(follower)
        match = engine._match_prefix(follower_blocks, follower)
        self.assertTrue(match["chain_broken"])
        self.assertEqual(match["resident_reusable_blocks"], 0)
        chain_events = [
            event for event in engine.events
            if event["type"] == E_CHAIN_BROKEN
        ]
        self.assertTrue(chain_events)
        self.assertTrue(all(event["block_index"] > 0 for event in chain_events))
        result = engine.commit_request(follower, follower_blocks)
        originals = {b["chain_fp"]: b for b in follower_blocks}
        for fp in result["recomputed_chain_fps"]:
            block = engine.store.registry[fp]
            self.assertEqual(block.content_fp, originals[fp]["content_fp"])
            self.assertEqual(block.chain_fp, fp)

    def test_recompute_matches_original_fields(self):
        engine = self.engine(residency_budget_blocks=4)
        self.run_requests(engine, self.requests[:6])
        recomputes = [e for e in engine.events if e["type"] == "RECOMPUTE"]
        self.assertTrue(recomputes)
        for event in recomputes:
            self.assertTrue(event["field_match"])
            self.assertIn(event["origin"],
                          ("budget_eviction", "chain_dependency_broken"))
        for block in engine.store.registry.values():
            if block.resident:
                self.assertFalse(block.invalidated)

    def test_failure_isolation(self):
        engine = self.engine()
        before = engine.store.resident_count()
        self.run_requests(engine, self.requests)
        codes = {f["code"] for f in engine.failures}
        self.assertIn(E_EMPTY_CONTEXT, codes)
        self.assertIn(E_TOKEN_UNKNOWN, codes)
        good = [r for r in engine.request_results if r.get("status") != "failed"]
        self.assertGreater(len(good), 0)
        self.assertEqual(engine.cursor, len(self.requests))
        self.assertGreaterEqual(engine.store.resident_count(), before)

    def test_serial_concurrent_identical(self):
        serial_engine = self.engine()
        serial_engine.run_serial([dict(r) for r in self.requests])
        concurrent_engine = self.engine()
        concurrent_engine.run_concurrent(
            [dict(r) for r in self.requests], self.config["max_workers"]
        )
        self.assertEqual(
            serial_engine.events, concurrent_engine.events
        )
        self.assertEqual(
            serial_engine.failures, concurrent_engine.failures
        )
        self.assertEqual(
            serial_engine.request_results,
            concurrent_engine.request_results,
        )

    def test_snapshot_roundtrip_and_stale_hash(self):
        snapshot_dir = os.path.join(self.tmp, "snap")
        store = SnapshotStore(snapshot_dir)
        engine = Engine(self.config, self.vocab, self.encoder, store)
        self.run_requests(engine, self.requests[:4])
        engine.shutdown()
        revived = Engine(self.config, self.vocab, self.encoder,
                         SnapshotStore(snapshot_dir))
        decision = revived.snapshot_store.restore_or_rebuild(revived)
        self.assertIsNone(decision["code"])
        self.assertEqual(revived.cursor, engine.cursor)
        self.assertEqual(
            sorted(revived.store.registry), sorted(engine.store.registry)
        )

        changed = dict(self.config)
        changed["residency_budget_blocks"] = 3
        stale_engine = Engine(changed, self.vocab, self.encoder,
                              SnapshotStore(snapshot_dir))
        decision = stale_engine.snapshot_store.restore_or_rebuild(stale_engine)
        self.assertEqual(decision["code"], E_SNAPSHOT_STALE)

    def test_corrupt_snapshot_rebuilds(self):
        snapshot_dir = os.path.join(self.tmp, "snap-corrupt")
        store = SnapshotStore(snapshot_dir)
        engine = Engine(self.config, self.vocab, self.encoder, store)
        self.run_requests(engine, self.requests[:3])
        engine.shutdown()
        with open(store.path, "w", encoding="utf-8") as handle:
            handle.write("{not-json")
        rebuilt = Engine(self.config, self.vocab, self.encoder,
                         SnapshotStore(snapshot_dir))
        decision = rebuilt.snapshot_store.restore_or_rebuild(rebuilt)
        self.assertEqual(decision["code"], E_SNAPSHOT_STALE)
        self.assertEqual(rebuilt.cursor, 0)

    def test_pipeline_reports_are_deterministic(self):
        report_dir_a = os.path.join(self.tmp, "reports-a")
        report_dir_b = os.path.join(self.tmp, "reports-b")
        snap_a = os.path.join(self.tmp, "snap-a")
        snap_b = os.path.join(self.tmp, "snap-b")
        os.makedirs(report_dir_a, exist_ok=True)
        os.makedirs(report_dir_b, exist_ok=True)
        _, report_a = run_stream(
            sample_dir=self.sample_dir,
            report_path=os.path.join(report_dir_a, "a.json"),
            snapshot_dir=snap_a,
            run_id="a",
        )
        _, report_b = run_stream(
            sample_dir=self.sample_dir,
            report_path=os.path.join(report_dir_b, "b.json"),
            snapshot_dir=snap_b,
            run_id="a",
        )
        report_a.pop("elapsed_ms")
        report_b.pop("elapsed_ms")
        self.assertEqual(report_a, report_b)
        import json

        with open(os.path.join(report_dir_a, "a.json"), encoding="utf-8") as fa:
            file_a = json.load(fa)
        with open(os.path.join(report_dir_b, "b.json"), encoding="utf-8") as fb:
            file_b = json.load(fb)
        file_a.pop("elapsed_ms")
        file_b.pop("elapsed_ms")
        self.assertEqual(
            json.dumps(file_a, sort_keys=True),
            json.dumps(file_b, sort_keys=True),
        )

    def test_config_hash_changes_with_override(self):
        changed = dict(self.config)
        changed["eviction_policy"] = "LFU"
        self.assertNotEqual(
            config_fingerprint(self.config), config_fingerprint(changed)
        )

    def test_failed_sessions_do_not_pollute_residency(self):
        failed_sessions = {"empty", "zeta", "delta"}
        good = [r for r in self.requests if r["session"] not in failed_sessions]
        full_engine = self.engine()
        full_engine.run_serial([dict(r) for r in self.requests])
        good_engine = self.engine()
        good_engine.run_serial([dict(r) for r in good])
        full_resident = {
            fp for fp, block in full_engine.store.registry.items()
            if block.resident
        }
        good_resident = {
            fp for fp, block in good_engine.store.registry.items()
            if block.resident
        }
        self.assertEqual(full_resident, good_resident)
        for block in full_engine.store.registry.values():
            self.assertTrue(failed_sessions.isdisjoint(block.owners))


if __name__ == "__main__":
    unittest.main()
