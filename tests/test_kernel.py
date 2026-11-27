from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dispatch_kernel import IncidentReuseKernel, generate_problem
from dispatch_kernel.engine import (
    evaluate_reference,
    relation_blob,
    relation_from_stored,
)
from dispatch_kernel.kernel import InjectedInterruption
from dispatch_kernel.plan import (
    Aggregate,
    Filter,
    Join,
    Project,
    build_plan,
    plan_fingerprint,
)
from dispatch_kernel.util import canonical_json


class KernelTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "dispatch.db"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def open_kernel(self, reset: bool = False) -> IncidentReuseKernel:
        return IncidentReuseKernel(self.db_path, seed=42, reset=reset)


class GeneratorTests(unittest.TestCase):
    def test_fixed_seed_is_identical(self) -> None:
        first = generate_problem(1234)
        second = generate_problem(1234)
        self.assertEqual(
            canonical_json(first.base_records),
            canonical_json(second.base_records),
        )
        self.assertEqual(
            canonical_json(first.queries),
            canonical_json(second.queries),
        )
        self.assertEqual(
            canonical_json(first.updates),
            canonical_json(second.updates),
        )
        self.assertEqual(
            canonical_json(first.vehicles),
            canonical_json(second.vehicles),
        )

    def test_seed_changes_data(self) -> None:
        self.assertNotEqual(
            canonical_json(generate_problem(1).base_records),
            canonical_json(generate_problem(2).base_records),
        )


class NormalizationTests(KernelTestCase):
    def test_three_versions_and_field_provenance(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            status = kernel.status()
            self.assertEqual(status["normalized_event_count"], 72)
            self.assertEqual(status["raw_event_counts"], {"v1": 24, "v2": 24, "v3": 24})
            seen_versions = set()
            for version in (1, 2, 3):
                row = kernel.store.conn.execute(
                    "SELECT incident_id FROM incidents WHERE source_version = ? ORDER BY incident_id LIMIT 1",
                    (version,),
                ).fetchone()
                report = kernel.normalization_report(row["incident_id"])["events"][0]
                fields = report["fields"]
                self.assertEqual(fields["source_version"]["source_version"], version)
                self.assertEqual(fields["incident_id"]["source_version"], version)
                self.assertEqual(fields["occurred_at_ms"]["source_version"], version)
                self.assertIn("persons", fields)
                self.assertIsInstance(fields["persons"]["value"], list)
                self.assertTrue(fields["address_path"]["source_path"])
                self.assertTrue(fields["event_type"]["source_path"])
                seen_versions.add(version)
            self.assertEqual(seen_versions, {1, 2, 3})

    def test_seconds_are_converted_to_milliseconds(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            row = kernel.store.conn.execute(
                """
                SELECT i.incident_id, i.occurred_at_ms, r.reported_epoch_s
                FROM incidents i JOIN raw_v1 r ON r.event_no = i.incident_id
                ORDER BY i.incident_id LIMIT 1
                """
            ).fetchone()
            self.assertEqual(row["occurred_at_ms"], row["reported_epoch_s"] * 1000)


class PlanningTests(KernelTestCase):
    def test_plan_has_all_required_node_types_and_is_stable(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            plan = kernel._plans["Q01"]
            types = set()

            def walk(node: object) -> None:
                types.add(type(node).__name__)
                if isinstance(node, Filter):
                    walk(node.child)
                elif isinstance(node, Join):
                    walk(node.left)
                    walk(node.right)
                elif isinstance(node, (Aggregate, Project)):
                    walk(node.child)

            walk(plan)
            self.assertTrue({"Filter", "Join", "Aggregate", "Project"} <= types)
            spec = kernel._specs["Q01"]
            self.assertEqual(plan_fingerprint(plan), plan_fingerprint(build_plan(spec)))
            self.assertEqual(plan_fingerprint(plan), plan_fingerprint(build_plan(spec)))

    def test_candidates_have_multiple_query_references_and_integer_weights(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            candidates = kernel.candidate_report()
            self.assertGreaterEqual(len(candidates), 5)
            self.assertTrue(any(item["selected"] for item in candidates))
            self.assertTrue(any(not item["selected"] for item in candidates))
            for item in candidates:
                query_ids = {ref["query_id"] for ref in item["refs"]}
                self.assertGreaterEqual(len(query_ids), 2)
                self.assertEqual(
                    item["weight"],
                    (item["reference_count"] - 1) * item["cost"]
                    - item["estimated_rows"],
                )
                self.assertIsInstance(item["weight"], int)
                self.assertIn(
                    "selected:" if item["selected"] else "rejected:",
                    item["reason"],
                )


class ExecutionTests(KernelTestCase):
    def test_initial_results_match_full_reference(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            report = kernel.latest_consistency_report()
            self.assertTrue(report["consistent"])
            self.assertEqual(report["differences"], [])
            stored = kernel.store.load_query_results()
            for query_id, plan in kernel._plans.items():
                incremental = relation_from_stored(
                    stored[query_id]["blob"],
                    stored[query_id]["lineage_json"],
                )
                reference = evaluate_reference(kernel.store, plan)
                self.assertEqual(relation_blob(incremental), relation_blob(reference))
            self.assertGreater(kernel.query_results()["Q01"]["row_count"], 0)

    def test_ledger_and_incident_lookup(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            ledger = kernel.ledger()
            selected = [row for row in ledger if row["selected"]]
            self.assertTrue(selected)
            for row in selected:
                self.assertEqual(row["reuse_count"], row["reference_count"] - 1)
                self.assertEqual(row["saved_cost"], row["reuse_count"] * row["cost"])
                self.assertGreater(row["saved_cost"], 0)
            for row in ledger:
                if not row["selected"]:
                    self.assertEqual(row["reuse_count"], 0)
                    self.assertEqual(row["saved_cost"], 0)

            query_row = kernel.query_results()["Q02"]["rows"][0]
            incident_id = query_row["incident_id"]
            matches = kernel.ledger_lookup(incident_id=incident_id)
            self.assertTrue(matches)
            self.assertTrue(all(item["incident_id"] == incident_id for item in matches))

            occurred_at_ms = query_row["occurred_at_ms"]
            time_matches = kernel.ledger_lookup(
                start_ms=occurred_at_ms,
                end_ms=occurred_at_ms + 1,
            )
            self.assertTrue(time_matches)


class IncrementalTests(KernelTestCase):
    def _selected_snapshot(self, kernel: IncidentReuseKernel) -> dict[str, tuple[str, bytes]]:
        blobs = kernel.materialized_bytes()
        return {
            item["candidate_id"]: (item["fingerprint"], blobs[item["fingerprint"]])
            for item in kernel.candidate_report()
            if item["selected"]
        }

    def test_query_edit_recomputes_only_affected_cse_bytes(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            kernel.apply_next_update()
            before = self._selected_snapshot(kernel)
            summary = kernel.apply_next_update()
            self.assertTrue(summary["consistent"])
            after = self._selected_snapshot(kernel)
            affected = set(summary["affected_cse"])
            unaffected = set(before) - affected - set(summary["retired_cse"])
            self.assertTrue(unaffected)
            for candidate_id in unaffected:
                self.assertIn(candidate_id, after)
                self.assertEqual(before[candidate_id][1], after[candidate_id][1])
            vectors = kernel.version_vectors()
            self.assertEqual(vectors["queries"]["Q03"]["query_version"], 2)
            for candidate_id in unaffected:
                vector = vectors["candidates"][candidate_id]["vector"]
                self.assertNotIn("Q03", vector["queries"])

    def test_all_updates_and_restart_match_reference(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            summaries = kernel.apply_updates()
            self.assertEqual([item["seq"] for item in summaries], [1, 2, 3, 4])
            self.assertTrue(all(item["consistent"] for item in summaries))
            self.assertEqual(kernel.status()["normalized_event_count"], 78)
            report = kernel.verify()
            self.assertTrue(report["consistent"])
            self.assertEqual(report["differences"], [])
            vectors = kernel.version_vectors()
            self.assertEqual(vectors["data_versions"]["event.v1"], 2)
            self.assertEqual(vectors["data_versions"]["event.v3"], 2)
            self.assertEqual(
                vectors["queries"]["Q05"]["query_version"],
                2,
            )

        with self.open_kernel() as reopened:
            self.assertEqual(reopened.status()["last_update_seq"], 4)
            self.assertIsNone(reopened.store.next_pending_update())
            self.assertTrue(reopened.verify()["consistent"])

    def test_batch_boundary_recovery(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            kernel.apply_next_update()
        with self.open_kernel() as kernel:
            with self.assertRaises(InjectedInterruption):
                kernel.apply_next_update(interrupt_after_commit=True)
        with self.open_kernel() as kernel:
            self.assertEqual(kernel.status()["last_update_seq"], 2)
            self.assertEqual(kernel.store.next_pending_update()["seq"], 3)
            resumed = kernel.apply_updates()
            self.assertEqual([item["seq"] for item in resumed], [3, 4])
            self.assertTrue(kernel.verify()["consistent"])

    def test_failed_transaction_rolls_back_to_pending_batch(self) -> None:
        with self.open_kernel(reset=True) as kernel:
            with mock.patch.object(
                kernel,
                "_build_consistency_report",
                side_effect=RuntimeError("simulated crash"),
            ):
                with self.assertRaises(RuntimeError):
                    kernel.apply_next_update()
            self.assertEqual(kernel.store.next_pending_update()["seq"], 1)
            self.assertEqual(kernel.store.raw_counts()["v1"], 24)
            self.assertEqual(
                kernel.normalized_data_versions()["event.v1"],
                1,
            )
            self.assertEqual(kernel.status()["last_update_seq"], 0)
        with self.open_kernel() as reopened:
            self.assertEqual(reopened.store.next_pending_update()["seq"], 1)
            self.assertEqual(len(reopened.apply_updates()), 4)
            self.assertTrue(reopened.verify()["consistent"])


class CliTests(KernelTestCase):
    def test_cli_status_and_candidates(self) -> None:
        command = [
            sys.executable,
            "main.py",
            "--db",
            str(self.db_path),
            "--seed",
            "42",
            "--reset",
            "--compact",
            "status",
        ]
        status_process = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(status_process.returncode, 0, status_process.stderr)
        status = json.loads(status_process.stdout)
        self.assertEqual(status["normalized_event_count"], 72)

        candidates_process = subprocess.run(
            command[:-1] + ["candidates"],
            check=False,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.assertEqual(
            candidates_process.returncode,
            0,
            candidates_process.stderr,
        )
        candidates = json.loads(candidates_process.stdout)
        self.assertTrue(any(item["selected"] for item in candidates))


if __name__ == "__main__":
    unittest.main()
