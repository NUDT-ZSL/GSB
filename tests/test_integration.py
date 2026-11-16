import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path

from workbench.builder import GracefulStopRequested, IndexBuilder
from workbench.codec import canonical_block_hash
from workbench.engine import QueryEngine, QueryRequest
from workbench.normalization import parse_stream
from workbench.runner import compare_query_runs, run_concurrent, run_serial
from workbench.samplegen import write_samples
from workbench.store import (
    IndexConfig,
    IndexLayout,
    atomic_write_bytes,
    load_json,
    read_index_shard,
    reset_dirs,
    version_fingerprint,
)


def _config(sample_dir: Path, **overrides) -> IndexConfig:
    data = {
        "vocab_path": str(sample_dir / "vocab.txt"),
        "doc_shard_size": 25,
        "block_size": 8,
        "compression_level": 1,
        "store_positions": True,
        "query_concurrency": 4,
        "latency_budget_ms": 50,
        "low_freq_threshold": 2,
    }
    data.update(overrides)
    return IndexConfig.from_dict(data)


class WorkbenchIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample_dir = self.root / "samples"
        info = write_samples(self.sample_dir, doc_count=80, vocab_size=120)
        self.docs, self.parse_failures = parse_stream(
            Path(info["corpus_path"]).read_text(encoding="utf-8").splitlines()
        )
        self.queries = load_json(info["queries_path"])["queries"]

    def tearDown(self):
        self.temp.cleanup()

    def _requests(self):
        return [
            QueryRequest(
                query_id=item["query_id"],
                terms=item["terms"],
                sort_key=item["sort_key"],
                want_positions=item.get("want_positions", True),
            )
            for item in self.queries
        ]

    def test_parse_failure_codes(self):
        codes = [item["code"] for item in self.parse_failures]
        self.assertIn("E_PARSE_INVALID", codes)
        self.assertIn("E_EMPTY_DOC", codes)
        self.assertIn("E_DUP_DOC_ID", codes)

    def test_build_manifest_and_atomic_shards(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        result = builder.build(self.docs)
        builder.restore_signal_handlers()
        manifest = result.manifest
        self.assertTrue(manifest["version_fingerprint"])
        self.assertEqual(manifest["doc_count"], len(self.docs))
        self.assertTrue(manifest["doc_shards"])
        self.assertTrue(manifest["index_shards"])
        for shard in manifest["doc_shards"] + manifest["index_shards"]:
            path = layout.base_dir / shard["path"]
            self.assertTrue(path.exists())
            self.assertFalse(list(path.parent.glob(".*.tmp-*")))
        # 分片可被独立读回，块数量与清单一致
        total_blocks = 0
        for shard in manifest["index_shards"]:
            total_blocks += len(read_index_shard(
                layout.index_shard_path(shard["shard_id"])
            ))
        self.assertEqual(total_blocks, len(manifest["blocks"]))

    def test_version_fingerprint_changes_with_format(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx")
        reset_dirs(layout)
        first = IndexBuilder(config, layout)
        result_one = first.build(self.docs)
        first.restore_signal_handlers()
        fp_one = result_one.manifest["version_fingerprint"]

        switched = IndexBuilder(config.with_format(2, True), layout)
        mismatch = switched.detect_version_mismatch()
        self.assertIsNotNone(mismatch)
        self.assertIn("compression_level", mismatch["mismatched_fields"])
        result_two = switched.build()
        switched.restore_signal_handlers()
        self.assertNotEqual(fp_one, result_two.manifest["version_fingerprint"])
        self.assertTrue(result_two.manifest["block_reuse"]["rebuilt"])
        self.assertFalse(result_two.manifest["block_reuse"]["reused"])

    def test_incremental_reuses_unchanged_blocks(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        first = builder.build(self.docs[:20])
        second = builder.build(self.docs[20:])
        builder.restore_signal_handlers()
        reused_keys = {item["block_key"] for item in second.reused_blocks}
        rebuilt_terms = {
            item["block_key"].split("#")[0] for item in second.rebuilt_blocks
        }
        self.assertTrue(reused_keys)
        # 首批独有的低频词块应可被原样复用
        first_terms = set()
        for doc in self.docs[:20]:
            first_terms.update(doc.term_freq)
        self.assertTrue(any(key.split("#")[0] in first_terms
                            for key in reused_keys))
        self.assertTrue(rebuilt_terms)

    def test_incremental_equals_full_all_combos(self):
        requests = self._requests()
        for level in (1, 2):
            for store_positions in (False, True):
                with self.subTest(level=level, store_positions=store_positions):
                    config = _config(
                        self.sample_dir,
                        compression_level=level,
                        store_positions=store_positions,
                    )
                    inc_layout = IndexLayout(self.root / f"inc-{level}-{store_positions}")
                    full_layout = IndexLayout(self.root / f"full-{level}-{store_positions}")
                    reset_dirs(inc_layout)
                    reset_dirs(full_layout)

                    inc_builder = IndexBuilder(config, inc_layout)
                    for start in range(0, len(self.docs), 10):
                        inc_builder.build(self.docs[start:start + 10])
                    inc_builder.restore_signal_handlers()

                    full_builder = IndexBuilder(config, full_layout)
                    full_builder.build(self.docs)
                    full_builder.restore_signal_handlers()

                    inc_results = [
                        QueryEngine(inc_layout, config.latency_budget_ms)
                        .run(request).canonical()
                        for request in requests
                    ]
                    full_results = [
                        QueryEngine(full_layout, config.latency_budget_ms)
                        .run(request).canonical()
                        for request in requests
                    ]
                    self.assertEqual(inc_results, full_results)
                    self.assertEqual(
                        load_json(inc_layout.manifest_path)["version_fingerprint"],
                        load_json(full_layout.manifest_path)["version_fingerprint"],
                    )

    def test_concurrent_matches_serial(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx-conc")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs)
        builder.restore_signal_handlers()
        requests = self._requests()
        factory = lambda: QueryEngine(layout, config.latency_budget_ms)
        serial = run_serial(requests, factory)
        concurrent = run_concurrent(requests, factory, config.query_concurrency)
        comparison = compare_query_runs(serial, concurrent)
        self.assertTrue(comparison["equal"], comparison["mismatches"])
        self.assertEqual(
            [item.sort_key for item in concurrent.ordered()],
            [item.sort_key for item in serial.ordered()],
        )

    def test_budget_exhausted_still_returns_full_results(self):
        config = _config(self.sample_dir, latency_budget_ms=0)
        # budget 0 时任何查询都会标注超预算，但结果仍完整
        layout = IndexLayout(self.root / "idx-budget")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs)
        builder.restore_signal_handlers()
        request = QueryRequest("budget-q", ["alpha"], 0, True)
        response = QueryEngine(layout, 0).run(request)
        self.assertTrue(response.budget_exhausted)
        self.assertTrue(response.doc_ids)
        self.assertEqual(response.doc_ids, sorted(response.doc_ids))

    def test_graceful_stop_commits_then_resume(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx-stop")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs[:15])
        committed_progress = load_json(layout.progress_path)
        self.assertEqual(committed_progress["phase"], "committed")
        builder.request_stop()
        with self.assertRaises(GracefulStopRequested):
            builder.build(self.docs[15:30])
        builder.restore_signal_handlers()
        # 已提交产物与进度存在
        self.assertTrue(layout.manifest_path.exists())
        self.assertTrue(layout.progress_path.exists())
        manifest = load_json(layout.manifest_path)
        self.assertEqual(manifest["doc_count"], 15)

        resume = IndexBuilder(config, layout)
        self.assertTrue(resume.load_committed_state())
        result = resume.build(self.docs[15:])
        resume.restore_signal_handlers()
        self.assertEqual(result.manifest["doc_count"], len(self.docs))

    def test_atomic_write_replaces_content(self):
        target = self.root / "atomic.bin"
        atomic_write_bytes(target, b"one")
        atomic_write_bytes(target, b"two")
        self.assertEqual(target.read_bytes(), b"two")


if __name__ == "__main__":
    unittest.main()
