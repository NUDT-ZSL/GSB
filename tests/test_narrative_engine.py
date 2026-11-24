from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from narrative_engine.branches import explore_branches
from narrative_engine.checkpoint import CheckpointStore, verify_snapshot_file
from narrative_engine.cli import main as cli_main
from narrative_engine.concurrency import WorkChunk, run_bounded_chunks
from narrative_engine.encoding import canonical_digest
from narrative_engine.errors import DigestError, SchemaError
from narrative_engine.generator import generate_sample
from narrative_engine.reporting import verify_report_correspondence
from narrative_engine.schema import load_choice_file, load_script, parse_script_object


class SampleWorkspace:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = generate_sample(self.root / "samples", seed=351)
        self.script_path = self.paths["bundle"]
        self.story_ref_path = self.paths["story"]
        self.choices_path = self.paths["choices"]
        self.sequences_path = self.paths["sequences"]

    def cleanup(self) -> None:
        self.temp.cleanup()


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = SampleWorkspace()
        self.script = load_script(self.workspace.script_path)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def mutate(self, mutator):
        data = json.loads(self.workspace.script_path.read_text(encoding="utf-8"))
        mutator(data)
        return parse_script_object(data)

    def test_generated_sample_is_valid(self) -> None:
        self.assertEqual(self.script.scale, 1_000_000)
        self.assertIn("node_arrival", self.script.node_map)
        self.assertGreaterEqual(len(self.script.nodes), 9)

    def test_duplicate_node_id_rejected(self) -> None:
        def mutate(data):
            data["nodes"][1]["id"] = data["nodes"][0]["id"]

        with self.assertRaises(SchemaError) as ctx:
            self.mutate(mutate)
        self.assertEqual(ctx.exception.code, "E_SCHEMA")

    def test_undeclared_condition_and_effect_keys_rejected(self) -> None:
        def mutate(data):
            event = data["nodes"][0]["events"][-1]
            event.setdefault("preconditions", []).append(
                {"key": "missing_key", "op": ">=", "value": 1}
            )
            event["effects"].append({"key": "another_missing", "op": "set", "value": True})

        with self.assertRaises(SchemaError) as ctx:
            self.mutate(mutate)
        issues = ctx.exception.details["issues"]
        locations = [issue["path"] + " " + issue["message"] for issue in issues]
        self.assertTrue(any("missing_key" in location for location in locations))
        self.assertTrue(any("another_missing" in location for location in locations))

    def test_missing_choice_target_rejected(self) -> None:
        def mutate(data):
            data["nodes"][0]["choices"][0]["target"] = "node_does_not_exist"

        with self.assertRaises(SchemaError):
            self.mutate(mutate)

    def test_threshold_outside_domain_rejected(self) -> None:
        def mutate(data):
            data["nodes"][0]["events"][8]["preconditions"][0]["value"] = 99

        with self.assertRaises(SchemaError):
            self.mutate(mutate)

    def test_non_finite_threshold_rejected(self) -> None:
        data = json.loads(self.workspace.script_path.read_text(encoding="utf-8"))
        data["nodes"][0]["events"][8]["preconditions"][0]["value"] = "Infinity"
        bad = self.workspace.root / "bad.json"
        bad.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SchemaError):
            load_script(bad)

    def test_invalid_cli_input_writes_no_outputs(self) -> None:
        bad_script = self.workspace.root / "bad-cli.json"
        data = json.loads(self.workspace.script_path.read_text(encoding="utf-8"))
        data["start_node"] = "missing"
        bad_script.write_text(json.dumps(data), encoding="utf-8")
        state_dir = self.workspace.root / "never-state"
        report = self.workspace.root / "nested" / "report.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "run",
                    "--script",
                    str(bad_script),
                    "--choices",
                    str(self.workspace.choices_path),
                    "--state-dir",
                    str(state_dir),
                    "--report",
                    str(report),
                ]
            )
        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "E_SCHEMA")
        self.assertFalse(state_dir.exists())
        self.assertFalse(report.parent.exists())


class FixedPointCausalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = SampleWorkspace()
        self.script = load_script(self.workspace.script_path)
        self.choices = load_choice_file(self.workspace.choices_path)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def run_cli_report(self, directory: Path, *extra: str):
        state = directory / "state"
        report = directory / "report.json"
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "run",
                    "--script",
                    str(self.workspace.script_path),
                    "--choices",
                    str(self.workspace.choices_path),
                    "--state-dir",
                    str(state),
                    "--report",
                    str(report),
                    *extra,
                ]
            )
        return code, report, json.loads(stdout.getvalue())

    def test_fixed_float_residual_and_causal_chain(self) -> None:
        code, report_path, _ = self.run_cli_report(self.workspace.root)
        self.assertEqual(code, 0)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "terminal")
        self.assertEqual(report["world_state_exact"]["fixed"]["trust"], 1_050_000)

        divergence = report["first_residual_exceedance"]
        self.assertEqual(divergence["event_seq"], 8)
        self.assertEqual(divergence["state_key"], "trust")
        self.assertEqual(divergence["node_id"], "node_arrival")
        self.assertGreater(divergence["absolute_difference"], 1e-16)

        gate = next(
            event
            for event in report["event_records"]
            if event["event_id"] == "evt_decode_eight_echoes"
        )
        condition = gate["condition_evaluations"][0]
        self.assertTrue(condition["passed"])
        self.assertFalse(condition["float64_reference"]["would_pass"])
        self.assertEqual(condition["float64_reference"]["actual"], 0.7999999999999999)

        errors = verify_report_correspondence(report, self.script)
        self.assertEqual(errors, [])
        event_numbers = {event["seq"] for event in report["event_records"]}
        for item in report["causal_chain"]:
            if item["source_type"] == "event":
                self.assertIn(item["event_seq"], event_numbers)
                self.assertEqual(item["event_ref"], f"event:{item['event_seq']}")
                self.assertIn("node_arrival", item["node_sequence"])
        post_choice = [
            item
            for item in report["causal_chain"]
            if "choice_code_vault" in item.get("upstream_choices", [])
        ]
        self.assertTrue(post_choice)
        self.assertIn("node_vault", post_choice[0]["node_sequence"])

    def test_repeated_runs_are_byte_deterministic(self) -> None:
        first_dir = self.workspace.root / "a"
        second_dir = self.workspace.root / "b"
        _, first_path, _ = self.run_cli_report(first_dir)
        _, second_path, _ = self.run_cli_report(second_dir)
        first = json.loads(first_path.read_text(encoding="utf-8"))
        second = json.loads(second_path.read_text(encoding="utf-8"))
        for report in (first, second):
            report.pop("recovery", None)
        self.assertEqual(canonical_digest(first), canonical_digest(second))
        self.assertEqual(first["execution_order"], second["execution_order"])


class BranchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = SampleWorkspace()
        self.script = load_script(self.workspace.script_path)

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def test_branch_stats_deadlock_snapshot_and_order_invariance(self) -> None:
        forward = explore_branches(self.script)
        reverse = explore_branches(self.script, reverse_choice_order=True)
        self.assertEqual(forward["path_count"], 22)
        self.assertGreaterEqual(forward["deadlock_count"], 1)
        self.assertEqual([item["node_id"] for item in forward["deadlock_nodes"]], ["node_locked_archive"])

        def core(report):
            return [
                (
                    path["choice_ids"],
                    path["node_count"],
                    path["event_count"],
                    canonical_digest(path["world_state_exact"]),
                    path["deadlocked"],
                )
                for path in report["paths"]
            ]

        self.assertEqual(core(forward), core(reverse))
        deadlock_node = forward["deadlock_nodes"][0]
        snapshot = deadlock_node["fixed_state_snapshot"]["world_state_exact"]
        self.assertFalse(snapshot["booleans"]["archive_key"])
        self.assertFalse(snapshot["booleans"]["archive_secret"])
        self.assertEqual(snapshot["fixed"]["trust"], 800_000)


class CheckpointRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = SampleWorkspace()

    def tearDown(self) -> None:
        self.workspace.cleanup()

    def run_cli(self, *args: str):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "run",
                    "--script",
                    str(self.workspace.script_path),
                    "--choices",
                    str(self.workspace.choices_path),
                    *args,
                ]
            )
        return code, json.loads(stdout.getvalue())

    def test_interrupt_resume_and_deterministic_final_state(self) -> None:
        state = self.workspace.root / "state"
        report = self.workspace.root / "report.json"
        code, interrupted = self.run_cli(
            "--state-dir",
            str(state),
            "--report",
            str(report),
            "--stop-after-events",
            "3",
        )
        self.assertEqual(code, 130)
        self.assertEqual(interrupted["error"]["code"], "E_INTERRUPTED")
        self.assertEqual(interrupted["interrupt"]["event_seq"], 3)
        self.assertTrue((state / "interrupt.json").exists())
        self.assertEqual(len(list((state / "snapshots").glob("snapshot-*.json"))), 3)

        code, resumed = self.run_cli("--state-dir", str(state), "--report", str(report))
        self.assertEqual(code, 0)
        self.assertEqual(resumed["recovery"]["action"], "resumed")
        self.assertEqual(resumed["event_count"], 13)
        self.assertFalse((state / "interrupt.json").exists())
        self.assertEqual(verify_report_correspondence(resumed, load_script(self.workspace.script_path)), [])

    def test_digest_failure_falls_back_to_nearest_valid_snapshot(self) -> None:
        state = self.workspace.root / "state-digest"
        report = self.workspace.root / "digest-report.json"
        code, _ = self.run_cli(
            "--state-dir",
            str(state),
            "--report",
            str(report),
            "--stop-after-events",
            "5",
        )
        self.assertEqual(code, 130)
        current = state / "snapshot.json"
        body = json.loads(current.read_text(encoding="utf-8"))
        body["world_state"]["fixed"]["trust"] += 1
        current.write_text(json.dumps(body), encoding="utf-8")

        with self.assertRaises(DigestError):
            verify_snapshot_file(current)

        code, resumed = self.run_cli("--state-dir", str(state), "--report", str(report))
        self.assertEqual(code, 0)
        self.assertEqual(resumed["recovery"]["action"], "resumed")
        self.assertIn("snapshot-00000005.json", resumed["recovery"]["source"])
        self.assertTrue(resumed["warnings"])

    def test_declared_event_seq_mismatch_is_digest_error(self) -> None:
        state = self.workspace.root / "state-seq"
        report = self.workspace.root / "seq-report.json"
        self.run_cli(
            "--state-dir",
            str(state),
            "--report",
            str(report),
            "--stop-after-events",
            "2",
        )
        path = state / "snapshots" / "snapshot-00000002.json"
        body = json.loads(path.read_text(encoding="utf-8"))
        body["event_seq"] = 99
        from narrative_engine.checkpoint import snapshot_envelope

        path.write_text(json.dumps(snapshot_envelope(body)), encoding="utf-8")
        with self.assertRaises(DigestError) as ctx:
            verify_snapshot_file(path)
        self.assertEqual(ctx.exception.details["reason"], "event_seq_mismatch")

    def test_version_mismatch_archives_old_state(self) -> None:
        state = self.workspace.root / "state-version"
        report = self.workspace.root / "version-report.json"
        self.run_cli("--state-dir", str(state), "--report", str(report), "--stop-after-events", "2")

        changed = self.workspace.root / "changed-story.json"
        data = json.loads(self.workspace.script_path.read_text(encoding="utf-8"))
        data["nodes"][0]["title"] = "Changed title forces a new normalized version"
        changed.write_text(json.dumps(data), encoding="utf-8")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli_main(
                [
                    "run",
                    "--script",
                    str(changed),
                    "--choices",
                    str(self.workspace.choices_path),
                    "--state-dir",
                    str(state),
                    "--report",
                    str(report),
                ]
            )
        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["recovery"]["reason"], "version_mismatch")
        archive_root = state / "archive"
        self.assertTrue(archive_root.exists())
        self.assertTrue(any(archive_root.iterdir()))

    @unittest.skipIf(os.name == "nt", "POSIX signal delivery used by this subprocess test")
    def test_external_signal_exits_at_event_boundary(self) -> None:
        state = self.workspace.root / "signal-state"
        report = self.workspace.root / "signal-report.json"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "narrative_engine.cli",
                "run",
                "--script",
                str(self.workspace.script_path),
                "--choices",
                str(self.workspace.choices_path),
                "--state-dir",
                str(state),
                "--report",
                str(report),
                "--event-delay-ms",
                "250",
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(1.0)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, msg=stderr + stdout)
        payload = json.loads(stdout)
        self.assertEqual(payload["error"]["code"], "E_INTERRUPTED")
        self.assertGreaterEqual(payload["interrupt"]["event_seq"], 1)


class ConcurrencyTests(unittest.TestCase):
    def test_bounded_worker_count_and_ordered_results(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def worker(chunk: WorkChunk, cancel_event: threading.Event):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return chunk.payload * 2

        chunks = [WorkChunk(i, i, 1) for i in range(12)]
        results = run_bounded_chunks(chunks, worker, max_workers=3, memory_budget_bytes=1024)
        self.assertEqual(results, [i * 2 for i in range(12)])
        self.assertLessEqual(max_active, 3)
        self.assertEqual(max_active, 3)

    def test_memory_budget_is_respected(self) -> None:
        current = 0
        max_current = 0
        lock = threading.Lock()

        def worker(chunk: WorkChunk, cancel_event: threading.Event):
            nonlocal current, max_current
            with lock:
                current += chunk.estimated_bytes
                max_current = max(max_current, current)
            time.sleep(0.03)
            with lock:
                current -= chunk.estimated_bytes
            return True

        chunks = [WorkChunk(i, i, 4) for i in range(5)]
        run_bounded_chunks(chunks, worker, max_workers=4, memory_budget_bytes=8)
        self.assertLessEqual(max_current, 8)

    def test_first_chunk_error_cancels_others(self) -> None:
        started = threading.Event()
        finished = []
        lock = threading.Lock()

        def worker(chunk: WorkChunk, cancel_event: threading.Event):
            if chunk.index == 2:
                started.set()
                raise RuntimeError("first-boom")
            if chunk.index in {0, 1}:
                started.set()
                cancel_event.wait(timeout=2.0)
                with lock:
                    finished.append(chunk.index)
            return chunk.index

        chunks = [WorkChunk(i, i, 1) for i in range(8)]
        with self.assertRaises(RuntimeError) as ctx:
            run_bounded_chunks(chunks, worker, max_workers=2, memory_budget_bytes=1024)
        self.assertEqual(str(ctx.exception), "first-boom")
        deadline = time.time() + 2
        while time.time() < deadline and len(finished) < 2:
            time.sleep(0.02)
        self.assertLessEqual(len(finished), 2)

    def test_oversized_chunk_rejected(self) -> None:
        with self.assertRaises(Exception):
            run_bounded_chunks([WorkChunk(0, None, 10)], lambda c, e: None, memory_budget_bytes=4)


if __name__ == "__main__":
    unittest.main()
