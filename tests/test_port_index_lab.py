from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from port_index_lab.bptree import BPlusTree
from port_index_lab.compare import compare_hits
from port_index_lab.generator import (
    describe_edge_cases,
    generate_batches,
)
from port_index_lab.indexes import INDEX_DEFINITIONS
from port_index_lab.queries import full_table_baseline, generate_queries
from port_index_lab.runner import LabConfig, run_lab


class GeneratorTests(unittest.TestCase):
    def test_deterministic_batches_and_edge_cases(self) -> None:
        first = generate_batches(1234, 4, page_capacity=8)
        second = generate_batches(1234, 4, page_capacity=8)
        self.assertEqual(
            [[record.to_dict() for record in batch.records] for batch in first],
            [[record.to_dict() for record in batch.records] for batch in second],
        )
        records = [record for batch in first for record in batch.records]
        self.assertEqual(
            [record.write_seq for record in records],
            list(range(1, len(records) + 1)),
        )
        self.assertTrue(
            all(record.batch_id == batch.batch_id for batch in first for record in batch.records)
        )

        edge_cases = describe_edge_cases(first, 8)
        self.assertTrue(edge_cases["page_capacity_boundary"]["at_capacity"])
        self.assertEqual(len(first[0].records), 8)
        self.assertTrue(edge_cases["same_berth_time_groups"])
        self.assertGreater(edge_cases["zero_length_intervals"]["count"], 0)
        self.assertTrue(edge_cases["consecutive_duplicate_berth_runs"])


class BPlusTreeTests(unittest.TestCase):
    def _build_tree(self, definition, batches, capacity=8, fill_factor=0.5):
        tree = BPlusTree(
            definition,
            capacity=capacity,
            fill_factor=fill_factor,
            seed=77,
        )
        for batch in batches:
            tree.insert_many(batch.records)
        return tree

    def test_split_journal_and_structure(self) -> None:
        batches = generate_batches(77, 3, page_capacity=8)
        tree = self._build_tree(INDEX_DEFINITIONS[0], batches)
        structure = tree.validate_structure()
        self.assertTrue(structure["valid"], structure["issues"])
        self.assertGreater(tree.split_count, 0)
        event = tree.split_history[0]
        self.assertIn("page_id", event)
        self.assertIn("new_page_id", event)
        self.assertIn("level", event)
        self.assertIn("promoted_key", event)
        self.assertIn("before", event)
        self.assertIn("after", event)
        self.assertIn("left", event["after"])
        self.assertIn("right", event["after"])
        self.assertEqual(event["before"]["capacity"], 8)
        self.assertGreater(event["before"]["fill_ratio"], 1.0)
        self.assertTrue(structure["leaf_chain_connected"])

    def test_all_index_range_scans_match_full_table_baseline(self) -> None:
        batches = generate_batches(2026, 3, page_capacity=8)
        records = [record for batch in batches for record in batch.records]
        queries = generate_queries(records, 2026, 8)
        for definition in INDEX_DEFINITIONS:
            with self.subTest(index=definition.name):
                tree = self._build_tree(definition, batches)
                for query in queries:
                    scan = tree.scan_query(query)
                    baseline = full_table_baseline(records, query, definition)
                    comparison = compare_hits(
                        index_name=definition.name,
                        query=query,
                        baseline=baseline,
                        index_scan=scan,
                    )
                    self.assertTrue(comparison["consistent"], comparison)
                    self.assertTrue(scan["landing_correct"], scan["landing_details"])
                    self.assertTrue(scan["hits_sorted_by_index_key"])

    def test_snapshot_restore_then_incremental_matches_rebuild(self) -> None:
        batches = generate_batches(991, 3, page_capacity=8)
        records = [record for batch in batches for record in batch.records]
        queries = generate_queries(records, 991, 6)
        for definition in INDEX_DEFINITIONS:
            with self.subTest(index=definition.name):
                base = self._build_tree(definition, batches[:-1])
                snapshot = base.snapshot(
                    completed_batch=batches[-2].batch_id,
                )
                restored = BPlusTree.from_snapshot(
                    json.loads(json.dumps(snapshot, ensure_ascii=False)),
                    definition,
                )
                restored.insert_many(batches[-1].records)
                restored.last_completed_batch = batches[-1].batch_id

                rebuilt = self._build_tree(definition, batches)
                rebuilt.last_completed_batch = batches[-1].batch_id

                self.assertEqual(restored.fingerprint(), rebuilt.fingerprint())
                self.assertEqual(restored.split_count, rebuilt.split_count)
                self.assertTrue(
                    restored.validate_structure()["leaf_chain_connected"]
                )
                for query in queries:
                    restored_scan = restored.scan_query(query)
                    rebuilt_scan = rebuilt.scan_query(query)
                    self.assertEqual(
                        restored_scan["hit_record_ids"],
                        rebuilt_scan["hit_record_ids"],
                    )

    def test_broken_leaf_link_reports_specific_page(self) -> None:
        batches = generate_batches(15, 3, page_capacity=6)
        tree = self._build_tree(INDEX_DEFINITIONS[2], batches, capacity=6)
        leaves = [
            page for page in tree.pages.values() if page.is_leaf
        ]
        self.assertGreater(len(leaves), 1)
        first = tree.leftmost_leaf()
        first.next_leaf = 999_999
        validation = tree.validate_structure()
        self.assertFalse(validation["leaf_chain_connected"])
        issue_types = {
            issue["type"] for issue in validation["leaf_chain"]["issues"]
        }
        self.assertIn("forward_link_to_missing_page", issue_types)
        broken = [
            issue
            for issue in validation["leaf_chain"]["issues"]
            if issue["type"] == "forward_link_to_missing_page"
        ]
        self.assertEqual(broken[0]["page_id"], first.page_id)
        self.assertEqual(broken[0]["target_page_id"], 999_999)


class RunnerTests(unittest.TestCase):
    def test_end_to_end_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            report = run_lab(
                LabConfig(
                    seed=31,
                    batches=3,
                    page_capacity=6,
                    query_count=4,
                    output_dir=output,
                )
            )
            self.assertEqual(report["status"], "passed")
            self.assertTrue(report["acceptance_passed"])
            self.assertTrue(
                (output / "reports" / "final_report.json").is_file()
            )
            self.assertTrue(
                (output / "reports" / "acceptance_report.json").is_file()
            )
            snapshots = list((output / "snapshots").rglob("batch_*.json"))
            self.assertEqual(len(snapshots), 9)

    def test_interrupted_run_can_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            config = LabConfig(
                seed=32,
                batches=3,
                page_capacity=6,
                query_count=4,
                output_dir=output,
            )
            config.stop_after_batch = 2
            partial = run_lab(config)
            self.assertEqual(partial["status"], "partial")

            resumed_config = LabConfig(
                seed=32,
                batches=3,
                page_capacity=6,
                query_count=4,
                output_dir=output,
                resume=True,
            )
            final = run_lab(resumed_config)
            self.assertEqual(final["status"], "passed")
            self.assertTrue(
                final["acceptance_checks"][
                    "all_recovered_structures_match_continuous"
                ]
            )
            self.assertFalse(
                (output / "reports" / "partial_report.json").exists()
            )
            self.assertTrue(
                all(
                    len(metrics["batch_metrics"]) == 3
                    for metrics in final["index_comparison"]["metrics"]
                )
            )


if __name__ == "__main__":
    unittest.main()
