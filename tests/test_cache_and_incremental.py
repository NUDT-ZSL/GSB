from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from support_summary.cache import CacheStore
from support_summary.incremental import run_incremental_scenario
from support_summary.selfcheck import run_self_check
from support_summary.validation import (
    load_config,
    load_sessions,
    load_vocabulary,
)

from .helpers import make_dataset


class CacheAndIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = make_dataset(self.root)
        self.sessions = load_sessions(self.paths["sessions"])
        self.vocabulary = load_vocabulary(self.paths["vocabulary"])
        self.config = load_config(self.paths["config"])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _runner(self, cache_name: str):
        from support_summary.pipeline import PipelineRunner

        return PipelineRunner(
            self.sessions,
            self.vocabulary,
            self.config,
            cache_dir=self.root / "artifacts" / cache_name,
        )

    def test_cache_key_composition_and_reuse(self) -> None:
        first = self._runner("cache-reuse").run()
        self.assertGreater(first.cache_stats["miss"], 0)
        second = self._runner("cache-reuse").run()
        self.assertEqual(second.cache_stats["miss"], 0)
        self.assertGreater(second.cache_stats["hit"], 0)
        self.assertEqual(first.summary_bytes, second.summary_bytes)
        self.assertTrue(run_self_check(second)["ok"])

        objects = CacheStore(
            self.root / "artifacts" / "cache-reuse"
        ).query_session(self.sessions[0].session_id)
        self.assertTrue(objects)
        required = {
            "session_content_hash",
            "vocab_version",
            "rewrite_strength",
            "smoothing",
            "precision",
            "upstream_hash",
        }
        for item in objects:
            self.assertTrue(required <= set(item["key_components"]))
            self.assertEqual(len(item["content_hash"]), 64)
            self.assertEqual(len(item["cache_key"]), 64)

    def test_append_recomputes_only_new_block_suffix(self) -> None:
        report = run_incremental_scenario(
            self.sessions,
            self.vocabulary,
            self.config,
            cache_dir=self.root / "artifacts" / "cache-append",
            scenario="append",
            target_session_id=self.sessions[0].session_id,
        )
        self.assertTrue(report["ok"], report["self_check"])
        self.assertTrue(report["byte_identical_to_full"])
        self.assertEqual(
            report["affected_sessions"], [self.sessions[0].session_id]
        )
        self.assertEqual(
            report["stage_counts"]["chunk_tokenize"]["recomputed"], 1
        )
        self.assertEqual(
            report["stage_counts"]["unit_score"]["recomputed"], 1
        )
        self.assertEqual(
            report["stage_counts"]["rewrite"]["recomputed"], 2
        )
        self.assertEqual(
            report["stage_counts"]["summary_merge"]["recomputed"], 2
        )

    def test_strength_recomputes_rewrite_and_merge_only(self) -> None:
        report = run_incremental_scenario(
            self.sessions,
            self.vocabulary,
            self.config,
            cache_dir=self.root / "artifacts" / "cache-strength",
            scenario="strength",
            new_strength=0.65,
        )
        self.assertTrue(report["ok"], report["self_check"])
        self.assertTrue(report["byte_identical_to_full"])
        self.assertEqual(
            report["stage_counts"]["chunk_tokenize"]["recomputed"], 0
        )
        self.assertEqual(
            report["stage_counts"]["unit_score"]["recomputed"], 0
        )
        self.assertEqual(
            report["stage_counts"]["rewrite"]["reused"], 0
        )
        self.assertEqual(
            report["stage_counts"]["summary_merge"]["reused"], 0
        )

    def test_vocab_upgrade_recomputes_all_downstream(self) -> None:
        report = run_incremental_scenario(
            self.sessions,
            self.vocabulary,
            self.config,
            cache_dir=self.root / "artifacts" / "cache-vocab",
            scenario="vocab",
            vocabulary_path=self.paths["vocabulary_v2"],
        )
        self.assertTrue(report["ok"], report["self_check"])
        self.assertTrue(report["byte_identical_to_full"])
        for counts in report["stage_counts"].values():
            self.assertEqual(counts["reused"], 0)


if __name__ == "__main__":
    unittest.main()
