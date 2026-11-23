import copy
import tempfile
import unittest
from pathlib import Path

from xrel.canonical import canonical_bytes
from xrel.config import (
    AppConfig,
    Bm25Config,
    ExpandConfig,
    ResourceLimits,
    ThresholdConfig,
    default_config,
)
from xrel.concurrency import ChangeSet, StageLock
from xrel.errors import (
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    E_STALE_RESULT,
    E_VERSION_ROLLBACK,
    StageBusy,
    XrelError,
)
from xrel.data_gen import generate_bridge, generate_documents, generate_queries
from xrel.lineage import check_freshness, verify_lineage
from xrel.model import Document, DocTerm
from xrel.numerics import FixedConfig, RoundingMode
from xrel.pipeline import XrelPipeline
from xrel.scoring import replay_score
from xrel.snapshot import attach_checksum


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.bridge = generate_bridge(20261124, version=1)
        attach_checksum(self.bridge)
        self.documents = generate_documents(20261124)
        self.queries = generate_queries(20261124)
        self.pipeline = XrelPipeline(
            root / "snapshots", root / "results", default_config("float64")
        )
        self.pipeline.load_inputs(self.documents, self.queries, self.bridge)

    def tearDown(self):
        self.temp.cleanup()

    def test_three_tiers_share_hits_and_ranking(self):
        reference = self.pipeline.query_by_id("q-001", "float64").result
        for precision in ("float32", "fixed"):
            result = self.pipeline.query_by_id("q-001", precision).result
            self.assertEqual(
                set(reference["scores"]), set(result["scores"]), precision
            )
            ref_ids = [d["doc_id"] for d in reference["topk"]["selected"]]
            ids = [d["doc_id"] for d in result["topk"]["selected"]]
            self.assertEqual(ref_ids, ids, precision)

    def test_results_are_reproducible_byte_for_byte(self):
        first = self.pipeline.query_by_id("q-002", "fixed")
        second = self.pipeline.query_by_id("q-002", "fixed")
        self.assertEqual(canonical_bytes(first.result), canonical_bytes(second.result))

    def test_score_replay_from_contribution_detail(self):
        artifact = self.pipeline.run_query(self.queries[0], "float64")
        backend = self.pipeline  # ensure engine import path works
        from xrel.numerics import make_backend

        for doc_score in artifact.doc_scores.values():
            replayed = replay_score(
                doc_score.contributions,
                make_backend("float64"),
                self.pipeline.config.bm25.merge_rule,
            )
            self.assertAlmostEqual(replayed, doc_score.score, places=12)

    def test_error_spectrum_attributes_dominant_stage_for_fixed(self):
        record = self.pipeline.query_by_id("q-003", "fixed")
        spectrum = record.result["error_spectrum"]
        self.assertIn(spectrum["dominant_stage"], {
            "confidence_weighting",
            "partition_reduction",
            "quantization",
            "topk_threshold",
        })
        self.assertTrue(spectrum["fixed_recompute_ok"])
        for stages in spectrum["per_doc"].values():
            for stage in stages:
                self.assertLessEqual(stage["interval_low"], stage["interval_high"])
                self.assertGreaterEqual(stage["abs_deviation"], 0.0)

    def test_query_batch_consistency_self_check(self):
        report = self.pipeline.self_check_query_vs_batch("float32")
        self.assertTrue(report["consistent"])
        self.assertEqual(report["mismatches"], [])

    def test_stale_result_rejected_after_bridge_change(self):
        record = self.pipeline.query_by_id("q-000", "float64")
        new_bridge = copy.deepcopy(self.bridge)
        new_bridge.version = 2
        new_bridge.entries[("en", "en:t0004")] = [
            ("de", "de:t0004", 0.99),
            ("zh", "zh:词0004", 0.99),
        ]
        attach_checksum(new_bridge)
        change = ChangeSet(
            kind="bridge_entry",
            source_lang="en",
            source_terms=["en:t0004"],
            target_langs=["de", "zh"],
        )
        self.pipeline.commit_increment(change, new_bridge=new_bridge)
        with self.assertRaises(XrelError) as ctx:
            check_freshness(record, self.pipeline.snapshot_store)
        self.assertEqual(ctx.exception.code, E_STALE_RESULT)
        self.assertEqual(ctx.exception.context["query_id"], "q-000")
        self.assertIn("bridge", ctx.exception.context["stale_dependencies"])

    def test_incremental_bridge_change_recomputes_affected_suffix(self):
        new_bridge = copy.deepcopy(self.bridge)
        new_bridge.version = 2
        new_bridge.entries[("en", "en:t0004")] = [
            ("de", "de:t0004", 0.99),
            ("zh", "zh:词0004", 0.99),
        ]
        attach_checksum(new_bridge)
        change = ChangeSet(
            kind="bridge_entry",
            source_lang="en",
            source_terms=["en:t0004"],
            target_langs=["de", "zh"],
        )
        report = self.pipeline.commit_increment(change, new_bridge=new_bridge)
        verification = report["verification"]
        self.assertTrue(verification["recompute_reuse_disjoint"])
        self.assertTrue(verification["union_covers_all"])
        self.assertTrue(verification["byte_for_byte_equal_to_full_recompute"])
        self.assertIn("q-000", report["plan"]["recompute_queries"])

    def test_direction_addition_invalidates_source_language_queries(self):
        new_bridge = copy.deepcopy(self.bridge)
        new_bridge.version = 2
        new_bridge.directions = sorted(set(new_bridge.directions) | {("de", "en")})
        attach_checksum(new_bridge)
        change = ChangeSet(
            kind="direction",
            source_lang="de",
            target_langs=["en"],
        )
        report = self.pipeline.commit_increment(change, new_bridge=new_bridge)
        affected = set(report["plan"]["affected_queries"])
        german_queries = {q.query_id for q in self.queries if q.query_lang == "de"}
        self.assertTrue(german_queries <= affected)
        self.assertTrue(report["verification"]["byte_for_byte_equal_to_full_recompute"])

    def test_document_batch_invalidates_partition_queries(self):
        batch = [
            Document(
                doc_id="doc-en-900",
                doc_lang="en",
                terms=[DocTerm("en:t0001", 2), DocTerm("en:t0002", 1)],
                doc_len=3,
            )
        ]
        change = ChangeSet(kind="document_batch", doc_langs=["en"])
        report = self.pipeline.commit_increment(change, new_documents=batch)
        self.assertIn("en", report["plan"]["affected_partitions"])
        self.assertTrue(report["verification"]["union_covers_all"])

    def test_version_rollback_is_rejected(self):
        old_bridge = copy.deepcopy(self.bridge)
        old_bridge.version = 1
        attach_checksum(old_bridge)
        with self.assertRaises(XrelError) as ctx:
            self.pipeline.snapshot_store.publish_bridge(old_bridge)
        self.assertEqual(ctx.exception.code, E_VERSION_ROLLBACK)

    def test_stage_lock_is_non_blocking(self):
        lock = StageLock("resource_commit")
        lock.try_acquire()
        with self.assertRaises(StageBusy) as ctx:
            lock.try_acquire()
        self.assertEqual(ctx.exception.code, E_STAGE_BUSY)
        lock.release()
        lock.try_acquire()

    def test_resource_limit_raises_when_degrade_disabled(self):
        strict_config = AppConfig(
            precision="float64",
            fixed=FixedConfig(1_000_000, RoundingMode.HALF_EVEN),
            expand=ExpandConfig(),
            bm25=Bm25Config(),
            threshold=ThresholdConfig(),
            limits=ResourceLimits(
                max_posting_reads=1,
                max_candidate_comparisons=1,
                max_resident_detail_bytes=1_000_000,
                allow_streaming_degrade=False,
            ),
        )
        root = Path(self.temp.name) / "strict"
        strict_pipeline = XrelPipeline(root / "snap", root / "res", strict_config)
        strict_pipeline.load_inputs(self.documents, self.queries, self.bridge)
        with self.assertRaises(XrelError) as ctx:
            strict_pipeline.query_by_id("q-000", "float64", use_cache=False)
        self.assertEqual(ctx.exception.code, E_RESOURCE_LIMIT)

    def test_streaming_degrade_is_marked(self):
        degrade_config = default_config("float64")
        object.__setattr__(
            degrade_config,
            "limits",
            ResourceLimits(
                max_posting_reads=10,
                max_candidate_comparisons=10,
                max_resident_detail_bytes=10,
                allow_streaming_degrade=True,
            ),
        )
        root = Path(self.temp.name) / "degrade"
        pipeline = XrelPipeline(root / "snap", root / "res", degrade_config)
        pipeline.load_inputs(self.documents, self.queries, self.bridge)
        record = pipeline.query_by_id("q-000", "float64", use_cache=False)
        self.assertTrue(record.result["budget"]["degraded"])
        self.assertTrue(record.result["budget"]["degrade_reasons"])

    def test_lineage_break_detected_on_tampered_result(self):
        record = self.pipeline.query_by_id("q-001", "float64")
        record.result["scores"] = {}
        with self.assertRaises(XrelError) as ctx:
            verify_lineage(record)
        self.assertEqual(ctx.exception.code, "E_LINEAGE_BROKEN")


if __name__ == "__main__":
    unittest.main()
