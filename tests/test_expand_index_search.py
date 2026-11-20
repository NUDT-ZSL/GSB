from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qxworkbench.bridge import affected_partitions, parse_bridge
from qxworkbench.data import Query, read_queries
from qxworkbench.errors import WorkbenchError
from qxworkbench.expand import ExpansionConfig, expand_query, expansion_bytes
from qxworkbench.models import BridgeEntry, BridgeResource, LanguagePair
from qxworkbench.rules import parse_rules
from qxworkbench.scoring import RetrievalConfig
from qxworkbench.snapshots import SnapshotStore
from qxworkbench.workbench import BuildConfig, build_and_commit, query_view

from support import prepare_samples


class ExpansionIndexSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.samples = prepare_samples(self.root)
        self.rules = parse_rules(self.samples / "rules_v3.json")
        self.bridge1 = parse_bridge(self.samples / "bridge_v1.json")
        self.bridge2 = parse_bridge(self.samples / "bridge_v2.json")
        self.bridge3 = parse_bridge(self.samples / "bridge_v3.json")
        self.queries = read_queries(self.samples / "queries.jsonl")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def query(self, query_id: str) -> Query:
        return next(query for query in self.queries if query.query_id == query_id)

    def test_expansion_is_sorted_filtered_and_byte_deterministic(self) -> None:
        query = self.query("q-zh-car")
        first = expand_query(
            query,
            self.rules,
            self.bridge1,
            config=ExpansionConfig(max_candidates=64, min_confidence=0.0),
        )
        second = expand_query(
            query,
            self.rules,
            self.bridge1,
            config=ExpansionConfig(max_candidates=64, min_confidence=0.0),
        )
        self.assertEqual(expansion_bytes(first), expansion_bytes(second))
        candidates = first["candidates"]
        self.assertEqual(
            candidates,
            sorted(
                candidates,
                key=lambda item: (
                    -item["confidence"],
                    item["target_term"],
                    item["target_lang"],
                    item["source_term"],
                    tuple(item["rule_ids"]),
                ),
            ),
        )
        fr_voiture = next(
            item
            for item in candidates
            if item["target_lang"] == "fr" and item["target_term"] == "voiture"
        )
        self.assertEqual(fr_voiture["depth"], 2)
        self.assertAlmostEqual(
            fr_voiture["bridge_confidence"],
            round(0.96 * 0.89, 12),
        )
        self.assertGreater(fr_voiture["bridge_confidence"], 0.70)

        limited = expand_query(
            query,
            self.rules,
            self.bridge1,
            config=ExpansionConfig(max_candidates=1, min_confidence=0.0),
        )
        self.assertEqual(len(limited["candidates"]), 1)
        filtered = expand_query(
            query,
            self.rules,
            self.bridge1,
            config=ExpansionConfig(max_candidates=64, min_confidence=0.90),
        )
        self.assertTrue(all(item["confidence"] >= 0.90 for item in filtered["candidates"]))

    def test_equal_confidence_ties_use_target_term_order(self) -> None:
        bridge = BridgeResource(
            version=1,
            format_version="bridge-v1",
            language_pairs=(LanguagePair("src", "dst"),),
            entries=(
                BridgeEntry("src", "dst", "x", "zeta", 0.8),
                BridgeEntry("src", "dst", "x", "alpha", 0.8),
            ),
            checksum="test",
        )
        v1 = parse_rules(self.samples / "rules_v1.rules")
        base_rule = v1.rules[0]
        from dataclasses import replace

        ruleset = replace(
            v1,
            format_version="v3",
            rules=(
                replace(
                    base_rule,
                    rule_id="tie",
                    source_lang="src",
                    source_term="x",
                    targets=(),
                ),
            ),
        )
        # Use the parser-produced model via a minimal structured round trip.
        from qxworkbench.models import RewriteRule, Target

        ruleset = replace(
            ruleset,
            rules=(
                RewriteRule(
                    rule_id="tie",
                    source_lang="src",
                    source_term="x",
                    targets=(Target("dst", "alpha"),),
                    bridge_confidence_weight=1.0,
                    max_expansion_depth=1,
                    enabled=True,
                ),
            ),
        )
        query = Query("q", "src", {"x": 1}, 5)
        result = expand_query(query, ruleset, bridge)
        self.assertEqual(
            [item["target_term"] for item in result["candidates"]],
            ["alpha", "zeta"],
        )

    def test_affected_partitions_only_include_changed_target_languages(self) -> None:
        self.assertEqual(affected_partitions(self.bridge1, self.bridge2), ("en", "fr"))
        self.assertEqual(affected_partitions(self.bridge2, self.bridge3), ("fr",))
        self.assertNotIn("es", affected_partitions(self.bridge1, self.bridge3))

    def test_partition_order_and_posting_provenance(self) -> None:
        store = SnapshotStore(self.root / "state")
        build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge1,
            index_version=1,
            config=BuildConfig(workers=2),
        )
        manifest, partitions = store.load_index_snapshot(1)
        self.assertEqual(manifest["posting_count"], sum(len(item["postings"]) for item in partitions.values()))
        for lang, partition in partitions.items():
            doc_ids = [doc["doc_id"] for doc in partition["docs"]]
            self.assertEqual(doc_ids, sorted(doc_ids))
            posting_order = [
                (posting["doc_id"], posting["term"])
                for posting in partition["postings"]
            ]
            self.assertEqual(posting_order, sorted(posting_order))
            for posting in partition["postings"]:
                self.assertIn(posting["doc_id"], doc_ids)
                self.assertIsInstance(posting["source"]["line"], int)
                self.assertTrue(Path(posting["source"]["source"]).exists())
            self.assertEqual(
                partition["bridge_checksum"],
                self.bridge1.checksum,
            )
        self.assertIn("es", partitions)

    def test_search_contributions_baseline_delta_and_streaming_match(self) -> None:
        store = SnapshotStore(self.root / "state")
        build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge1,
            index_version=1,
            config=BuildConfig(workers=2),
        )
        view = store.load_current()
        query = self.query("q-zh-car")
        resident = query_view(
            view,
            query,
            retrieval_config=RetrievalConfig(max_resident_bytes=10_000_000),
        )
        streaming = query_view(
            view,
            query,
            retrieval_config=RetrievalConfig(max_resident_bytes=1),
        )
        self.assertEqual(resident["resource_mode"], "resident")
        self.assertEqual(streaming["resource_mode"], "streaming")
        self.assertIn("DEGRADED_STREAMING_PARTITION_SCORING", streaming["degraded"])
        for key in ("baseline_results", "rewritten_results", "delta_spectrum"):
            self.assertEqual(resident[key], streaming[key])

        for section in ("baseline_results", "rewritten_results"):
            for row in resident[section]:
                expected = round(
                    sum(detail["contribution"] for detail in row["contributions"]),
                    12,
                )
                self.assertEqual(row["score"], expected)
                self.assertEqual(row["recomputed_score"], expected)
                self.assertTrue(row["contributions"])
        self.assertTrue(resident["delta_spectrum"])
        for row in resident["delta_spectrum"]:
            self.assertAlmostEqual(
                row["delta"],
                round(row["rewritten_score"] - row["baseline_score"], 12),
            )

    def test_query_resource_limit(self) -> None:
        store = SnapshotStore(self.root / "state")
        build_and_commit(
            store,
            documents_path=self.samples / "documents.jsonl",
            ruleset=self.rules,
            rule_version=1,
            bridge=self.bridge1,
            index_version=1,
            config=BuildConfig(workers=2),
        )
        with self.assertRaises(WorkbenchError) as caught:
            query_view(
                store.load_current(),
                self.query("q-zh-car"),
                retrieval_config=RetrievalConfig(max_postings=1),
            )
        self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")


if __name__ == "__main__":
    unittest.main()
