from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

from obs_telemetry.engine import (
    build_ledger,
    evaluate_workload,
    query_result,
    recheck_policy,
)
from obs_telemetry.generator import generate_workload
from obs_telemetry.sampling import apply_policy_patch
from obs_telemetry.store import (
    ProcessingInterrupted,
    TelemetryStore,
    build_store_report,
    process_workload,
)
from obs_telemetry.util import canonical_json, compact_json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "main.py")


class WorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.seed = 7
        cls.workload = generate_workload(cls.seed)
        cls.result = evaluate_workload(cls.workload)

    def trace(self, project_index: int, trace_id: str) -> dict:
        return next(
            trace
            for trace in self.result["projects"][project_index]["traces"]
            if trace["trace_id"] == trace_id
        )

    def test_generation_is_deterministic_and_cli_is_byte_stable(self) -> None:
        first = canonical_json(generate_workload(self.seed))
        second = canonical_json(generate_workload(self.seed))
        self.assertEqual(first, second)
        run_one = subprocess.run(
            [sys.executable, MAIN, "generate", "--seed", str(self.seed)],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        run_two = subprocess.run(
            [sys.executable, MAIN, "generate", "--seed", str(self.seed)],
            cwd=ROOT,
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(run_one, run_two)
        self.assertEqual(run_one.decode("utf-8"), first)

    def test_workload_contract_and_edge_cases(self) -> None:
        self.assertEqual(self.workload["format"], "observatory-telemetry-workload")
        self.assertEqual(len(self.workload["projects"]), 3)
        for project in self.workload["projects"]:
            self.assertEqual(len(project["jobs"]), 7)
            self.assertEqual(len(project["chain_definitions"]), 7)
            sequences = [event["sequence"] for event in project["events"]]
            self.assertEqual(sequences, sorted(sequences))
            self.assertEqual(len(sequences), len(set(sequences)))
        edges = self.workload["projects"][0]["edge_cases"]
        for key in (
            "terminal_error_trace_id",
            "duration_equal_threshold_trace_id",
            "head_drop_tail_rescue_trace_id",
            "duplicate_span_trace_id",
            "missing_parent_trace_id",
        ):
            self.assertIn(key, edges)

    def test_tree_restoration_and_malformed_diagnostics(self) -> None:
        project = self.result["projects"][0]
        duplicate = self.trace(0, project["edge_cases"]["duplicate_span_trace_id"])
        codes = [item["code"] for item in duplicate["restoration"]["diagnostics"]]
        self.assertIn("DUPLICATE_SPAN_IGNORED", codes)
        self.assertEqual(len(duplicate["restoration"]["duplicate_events"]), 1)
        definition = next(
            item
            for item in self.workload["projects"][0]["chain_definitions"]
            if item["trace_id"] == duplicate["trace_id"]
        )
        self.assertEqual(duplicate["span_count"], len(definition["nodes"]))

        missing_parent = self.trace(
            0, project["edge_cases"]["missing_parent_trace_id"]
        )
        codes = [item["code"] for item in missing_parent["restoration"]["diagnostics"]]
        self.assertIn("PARENT_NOT_FOUND", codes)
        self.assertTrue(
            any(
                node["missing"]
                for node in missing_parent["restoration"]["tree"]
            )
        )
        self.assertGreaterEqual(
            len(missing_parent["restoration"]["expected_missing_nodes"]), 1
        )

        missing_field_id = self.result["projects"][1]["edge_cases"][
            "missing_parent_field_trace_id"
        ]
        missing_field = self.trace(1, missing_field_id)
        codes = [item["code"] for item in missing_field["restoration"]["diagnostics"]]
        self.assertIn("PARENT_FIELD_MISSING", codes)
        self.assertIn("MULTIPLE_TRACE_ROOTS", codes)

        self_parent_id = self.result["projects"][1]["edge_cases"]["self_parent_trace_id"]
        self_parent = self.trace(1, self_parent_id)
        codes = [item["code"] for item in self_parent["restoration"]["diagnostics"]]
        self.assertIn("SELF_PARENT_BROKEN", codes)

        for trace in project["traces"]:
            path = trace["restoration"]["critical_path"]
            self.assertIsNotNone(path)
            self.assertTrue(path["span_ids"])
            self.assertEqual(
                path["total_duration_us"],
                sum(span["duration_us"] for span in path["spans"]),
            )

    def test_sampling_is_consistent_and_error_boundary_is_exact(self) -> None:
        for project in self.result["projects"]:
            for trace in project["traces"]:
                decision = trace["sample_decision"]
                self.assertEqual(
                    decision["final"]["retained"],
                    decision["head"]["kept"] or decision["tail"]["kept"],
                )
                if decision["final"]["retained"]:
                    self.assertEqual(decision["final"]["decision"], "RETAIN")
                else:
                    self.assertEqual(decision["final"]["decision"], "DROP")
                    self.assertFalse(decision["head"]["kept"])
                    self.assertFalse(decision["tail"]["kept"])

        project = self.result["projects"][0]
        terminal = self.trace(
            0, project["edge_cases"]["terminal_error_trace_id"]
        )
        self.assertFalse(terminal["sample_decision"]["head"]["kept"])
        self.assertTrue(terminal["sample_decision"]["tail"]["kept"])
        self.assertTrue(terminal["sample_decision"]["final"]["tail_rescued"])
        self.assertTrue(terminal["failure_analysis"]["root_cause"]["is_observed_leaf"])
        self.assertTrue(
            any(
                reason["code"] == "TAIL_ERROR"
                for reason in terminal["sample_decision"]["tail"]["reasons"]
            )
        )

        threshold_trace = self.trace(
            0, project["edge_cases"]["duration_equal_threshold_trace_id"]
        )
        threshold_ms = project["policy"]["tail_duration_threshold_ms"]
        root = next(
            span
            for span in threshold_trace["restoration"]["flat_spans"]
            if span["effective_parent_span_id"] is None
        )
        self.assertEqual(root["duration_us"], threshold_ms * 1_000)
        self.assertFalse(
            any(
                reason["code"] == "TAIL_DURATION"
                for reason in threshold_trace["sample_decision"]["tail"]["reasons"]
            )
        )

        lowered = apply_policy_patch(
            self.workload,
            {"tail_duration_threshold_ms": threshold_ms - 1},
        )
        lowered_result = evaluate_workload(lowered)
        lowered_trace = next(
            trace
            for trace in lowered_result["projects"][0]["traces"]
            if trace["trace_id"] == threshold_trace["trace_id"]
        )
        self.assertTrue(
            any(
                reason["code"] == "TAIL_DURATION"
                for reason in lowered_trace["sample_decision"]["tail"]["reasons"]
            )
        )
        self.assertTrue(lowered_trace["sample_decision"]["final"]["retained"])

    def test_failure_root_cause_has_downstream_missing_nodes(self) -> None:
        project = self.result["projects"][1]
        internal = self.trace(
            1, project["edge_cases"]["internal_error_trace_id"]
        )
        self.assertTrue(internal["failure_analysis"]["failed"])
        self.assertGreaterEqual(
            len(internal["failure_analysis"]["downstream_missing_nodes"]), 1
        )
        root_cause = internal["failure_analysis"]["root_cause"]
        self.assertEqual(root_cause["status"].upper(), "ERROR")
        query = query_result(
            self.result,
            job_ids=[internal["job_id"]],
        )
        self.assertEqual(len(query["failures"]), 1)
        failure = query["failures"][0]
        self.assertEqual(failure["trace_id"], internal["trace_id"])
        self.assertTrue(
            all(
                node["span_id"]
                for node in failure["downstream_missing_nodes"]
            )
        )
        self.assertTrue(failure["critical_path"]["by_service"])

    def test_query_filters_before_aggregation(self) -> None:
        source = self.trace(0, self.result["projects"][0]["traces"][0]["trace_id"])
        start = int(source["trace_start_us"])
        query = query_result(
            self.result,
            job_ids=[source["job_id"]],
            from_us=start,
            to_us=start + 1,
            include_dropped=True,
        )
        self.assertEqual(query["summary"]["trace_count"], 1)
        self.assertEqual(len(query["jobs"]), 1)
        self.assertEqual(query["jobs"][0]["job_id"], source["job_id"])
        self.assertTrue(
            all(trace["job_id"] == source["job_id"] for trace in query["traces"])
        )
        empty = query_result(self.result, job_ids=[source["job_id"]], from_us=start + 1)
        self.assertEqual(empty["summary"]["trace_count"], 0)

    def test_ledger_reconciles_with_per_trace_decisions(self) -> None:
        ledger = build_ledger(self.result)
        expected_traces = sum(
            1
            for project in self.result["projects"]
            for trace in project["traces"]
            if trace["retained"]
        )
        expected_spans = sum(
            trace["span_count"]
            for project in self.result["projects"]
            for trace in project["traces"]
            if trace["retained"]
        )
        expected_bytes = sum(
            trace["attribute_bytes"]
            for project in self.result["projects"]
            for trace in project["traces"]
            if trace["retained"]
        )
        self.assertEqual(ledger["totals"]["retained_trace_count"], expected_traces)
        self.assertEqual(ledger["totals"]["retained_span_count"], expected_spans)
        self.assertEqual(ledger["totals"]["attribute_bytes"], expected_bytes)
        self.assertTrue(ledger["reconciliation"]["all_equal"])

        for trace in ledger["trace_decisions"]:
            if trace["retained"]:
                self.assertEqual(
                    trace["expired"],
                    trace["expires_at_us"] <= self.project_now(trace["project_id"]),
                )

        first_job = self.result["projects"][0]["jobs"][0]["job_id"]
        filtered = build_ledger(self.result, job_ids=[first_job])
        self.assertTrue(all(job["job_id"] == first_job for job in filtered["jobs"]))
        self.assertTrue(filtered["reconciliation"]["all_equal"])

    def project_now(self, project_id: str) -> int:
        return next(
            int(project["time_origin"]["now_us"])
            for project in self.result["projects"]
            if project["project_id"] == project_id
        )

    def test_incremental_rechecks_equal_full_rechecks(self) -> None:
        patches = [
            {"head_sampling_rate_basis_points": 9_000},
            {"tail_duration_threshold_ms": 1_500},
            {"tail_error_enabled": False},
            {"tail_duration_comparison": "gte"},
            {
                "retention_hours_by_job": {
                    self.result["projects"][0]["jobs"][0]["job_id"]: 1
                }
            },
        ]
        base_decisions = {
            trace["trace_id"]: trace["sample_decision"]
            for project in self.result["projects"]
            for trace in project["traces"]
        }
        for patch in patches:
            with self.subTest(patch=patch):
                recheck = recheck_policy(self.workload, patch)
                self.assertTrue(recheck["consistency"]["all_equal"])
                self.assertEqual(recheck["consistency"]["mismatch_count"], 0)
                adjusted_workload = apply_policy_patch(self.workload, patch)
                full_result = evaluate_workload(adjusted_workload)
                self.assertEqual(
                    recheck["adjusted_result"]["result_sha256"],
                    full_result["result_sha256"],
                )
                adjusted_decisions = {
                    trace["trace_id"]: trace["sample_decision"]
                    for project in recheck["adjusted_result"]["projects"]
                    for trace in project["traces"]
                }
                for trace_id in recheck["reused_trace_ids"]:
                    self.assertEqual(
                        compact_json(base_decisions[trace_id]),
                        compact_json(adjusted_decisions[trace_id]),
                    )
                self.assertTrue(
                    all(recheck["before_ledger"]["totals"][key] >= 0 for key in (
                        "retained_trace_count",
                        "retained_span_count",
                        "attribute_bytes",
                    ))
                )

    def test_sqlite_interruption_resume_and_idempotence(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = os.path.join(tempdir, "telemetry.sqlite3")
            with TelemetryStore(db_path) as store:
                store.initialize(self.workload, reset=True)
                with self.assertRaises(ProcessingInterrupted):
                    process_workload(store, self.workload, interrupt_after=4)
                self.assertEqual(store.committed_count(), 4)
                progress = process_workload(store, self.workload)
                self.assertEqual(progress["newly_committed_trace_count"], 17)
                self.assertEqual(progress["skipped_committed_trace_count"], 4)
                report = build_store_report(store, self.workload)
                self.assertEqual(report["result"], self.result)
                before = store.committed_count()
                noop = process_workload(store, self.workload)
                self.assertEqual(noop["newly_committed_trace_count"], 0)
                self.assertEqual(store.committed_count(), before)

    def test_cli_restore_sample_query_ledger_and_recheck(self) -> None:
        commands = [
            ["restore", "--seed", str(self.seed)],
            ["sample", "--seed", str(self.seed)],
            ["query", "--seed", str(self.seed)],
            ["ledger", "--seed", str(self.seed)],
            ["recheck", "--seed", str(self.seed), "--head-rate-bps", "8000"],
        ]
        for command in commands:
            with self.subTest(command=command):
                completed = subprocess.run(
                    [sys.executable, MAIN, *command],
                    cwd=ROOT,
                    check=True,
                    capture_output=True,
                )
                parsed = json.loads(completed.stdout)
                self.assertIn("format", parsed)

    def test_cli_persist_and_resume_match_run_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = os.path.join(tempdir, "cli.sqlite3")
            one_shot = subprocess.run(
                [sys.executable, MAIN, "run", "--seed", str(self.seed)],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            interrupted = subprocess.run(
                [
                    sys.executable,
                    MAIN,
                    "persist",
                    "--seed",
                    str(self.seed),
                    "--db",
                    db_path,
                    "--interrupt-after",
                    "3",
                ],
                cwd=ROOT,
                capture_output=True,
            )
            self.assertEqual(interrupted.returncode, 130)
            resumed = subprocess.run(
                [
                    sys.executable,
                    MAIN,
                    "resume",
                    "--seed",
                    str(self.seed),
                    "--db",
                    db_path,
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(resumed, one_shot)


if __name__ == "__main__":
    unittest.main()
