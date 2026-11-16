import json
import tempfile
import unittest
from pathlib import Path

from workbench.builder import IndexBuilder
from workbench.engine import QueryEngine, QueryRequest
from workbench.errors import E_DECODE_FAILED
from workbench.normalization import parse_stream
from workbench.samplegen import write_samples
from workbench.store import (
    IndexConfig,
    IndexLayout,
    atomic_write_bytes,
    load_json,
    reset_dirs,
)
from tests.test_integration import _config


class FailurePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sample_dir = self.root / "samples"
        info = write_samples(self.sample_dir, doc_count=60, vocab_size=100)
        self.docs, _ = parse_stream(
            Path(info["corpus_path"]).read_text(encoding="utf-8").splitlines()
        )
        self.info = info

    def tearDown(self):
        self.temp.cleanup()

    def test_tampered_shard_raises_decode_failure(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs)
        builder.restore_signal_handlers()

        manifest = load_json(layout.manifest_path)
        target = layout.base_dir / manifest["index_shards"][0]["path"]
        raw = target.read_bytes()
        atomic_write_bytes(target, raw[:-4] + b"\x00\x00\x00\x00")

        # 新会话恢复时必须识别已提交产物损坏，而不是静默复用
        recovery = IndexBuilder(config, layout)
        with self.assertRaises(Exception):
            recovery.load_committed_state()

    def test_query_exception_isolated_to_failure_list(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx2")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs)
        builder.restore_signal_handlers()
        engine = QueryEngine(layout, config.latency_budget_ms)
        good = QueryRequest("good", ["alpha"], 0, True)
        bad = QueryRequest("bad", [""], 1, True)
        good_response = engine.run(good)
        bad_response = engine.run(bad)
        self.assertTrue(good_response.doc_ids)
        self.assertEqual(bad_response.doc_ids, [])
        self.assertTrue(bad_response.failures)
        self.assertEqual(bad_response.failures[0]["stage"], "parse")
        self.assertIn("exception_type", bad_response.failures[0])

    def test_unknown_term_returns_empty_not_error(self):
        config = _config(self.sample_dir)
        layout = IndexLayout(self.root / "idx3")
        reset_dirs(layout)
        builder = IndexBuilder(config, layout)
        builder.build(self.docs)
        builder.restore_signal_handlers()
        response = QueryEngine(layout, config.latency_budget_ms).run(
            QueryRequest("none", ["zzz-never-appears-000"], 0, False)
        )
        self.assertEqual(response.doc_ids, [])
        self.assertEqual(response.failures, [])


if __name__ == "__main__":
    unittest.main()
