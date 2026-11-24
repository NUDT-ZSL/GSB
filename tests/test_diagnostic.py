from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from screen_diag.errors import SchemaError
from screen_diag.hashing import canonical_json_bytes, version_identity
from screen_diag.injection import prepare_injected_sample
from screen_diag.persistence import ChunkStore
from screen_diag.pipeline import analyze_sample, run_diagnostic
from screen_diag.replay import adjudicate_stream, canonical_replay_digest
from screen_diag.sample_generator import _on_rm_error, generate_sample_tree
from screen_diag.validation import parse_events, parse_manifest, relative_seconds_to_ms


class DiagnosticTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="screen-diag-test-"))
        cls.samples = generate_sample_tree(cls.root / "samples", seed=384)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, onerror=_on_rm_error)

    def _injected(self, case_id: str) -> Path:
        destination = self.root / "injected" / case_id
        path, _case = prepare_injected_sample(
            self.samples,
            case_id,
            destination=destination,
        )
        return path

    def test_01_baseline_pass_and_adjudication_counts(self) -> None:
        analysis = analyze_sample(self.samples, max_workers=2, memory_budget_bytes=65536)
        report = analysis["report"]
        self.assertEqual("PASS", report["status"])
        self.assertEqual(0, report["exit_code"])
        counts = report["statistics"]["decision_counts"]
        self.assertEqual(32, counts["total"])
        self.assertEqual(28, counts["adopted"])
        self.assertEqual(2, counts["late_dropped"])
        self.assertEqual(2, counts["generation_rollback"])

    def test_02_replay_is_byte_deterministic(self) -> None:
        manifest_raw = json.loads((self.samples / "sessions.json").read_text(encoding="utf-8"))
        events_raw = json.loads((self.samples / "frame_events.json").read_text(encoding="utf-8"))
        manifest = parse_manifest(manifest_raw)
        events = parse_events(events_raw, manifest)
        first = adjudicate_stream(manifest, events)
        second = adjudicate_stream(manifest, list(events))
        self.assertEqual(canonical_replay_digest(first), canonical_replay_digest(second))
        self.assertEqual(
            canonical_json_bytes(first.to_dict()),
            canonical_json_bytes(second.to_dict()),
        )
        for screen_id, log in first.states.items():
            self.assertEqual(log, second.states[screen_id])
            self.assertEqual(first.counts_by_screen[screen_id], second.counts_by_screen[screen_id])

    def test_03_merge_conflicts_are_located_to_shard_and_screen(self) -> None:
        analysis = analyze_sample(self.samples)
        conflicts = analysis["merged"]["conflicts"]
        self.assertGreaterEqual(len(conflicts), 12)
        keyframe_conflicts = [item for item in conflicts if item["field"] == "keyframe_seq"]
        self.assertTrue(keyframe_conflicts)
        for conflict in conflicts:
            self.assertTrue(conflict["canonical_screen_id"])
            self.assertTrue(conflict["source_locator"])
            self.assertIn("candidates", conflict)
        target = next(
            item
            for item in keyframe_conflicts
            if item["logic_ms"] == 5000
            and item["canonical_screen_id"] == "canon:display:living-room-big"
        )
        self.assertEqual("shard-001-cp4s", target["winner_shard_id"])
        self.assertEqual(9, target["selected_value"])

    def test_04_tombstone_wins_and_is_visible(self) -> None:
        analysis = analyze_sample(self.samples)
        current = analysis["merged"]["current"]
        living = current["canon:display:living-room-big"]
        self.assertTrue(living["tombstone"])
        self.assertEqual("shard-004-tombstone", living["shard_id"])
        tombstone_conflicts = [
            item
            for item in analysis["merged"]["conflicts"]
            if item["logic_ms"] == 8000
            and item["canonical_screen_id"] == "canon:display:living-room-big"
        ]
        self.assertTrue(tombstone_conflicts)
        self.assertTrue(all(item["adjudication"] == "tombstone_priority" for item in tombstone_conflicts))

    def test_05_low_memory_budget_still_loads_all_shards(self) -> None:
        analysis = analyze_sample(self.samples, max_workers=2, memory_budget_bytes=700)
        stats = analysis["merged"]["shard_load_stats"]
        self.assertGreaterEqual(stats["budget_wait_count"], 1)
        self.assertEqual(5, len(analysis["merged"]["shards"]))
        self.assertTrue(all(shard["valid"] for shard in analysis["merged"]["shards"]))
        self.assertEqual(
            sorted(stats["deterministic_order_keys"]),
            stats["deterministic_order_keys"],
        )

    def test_06_digest_corruption_rejects_shard_and_reports_fallback(self) -> None:
        injected = self._injected("corrupt_digest")
        analysis = analyze_sample(injected)
        report = analysis["report"]
        self.assertEqual("E_DIGEST", report["status"])
        self.assertEqual(1, len(report["merge"]["digest_failures"]))
        failure = report["merge"]["digest_failures"][0]
        self.assertEqual("shard-001-cp4s", failure["shard_id"])
        self.assertIn("digest does not match", failure["reason"])
        fallbacks = report["merge"]["shard_fallbacks"]
        self.assertEqual(2, len(fallbacks))
        self.assertTrue(all(item["available"] for item in fallbacks))
        self.assertEqual("shard-000-cp2s", fallbacks[0]["fallback_shard_id"])

    def test_07_screen_count_mismatch_is_e_digest(self) -> None:
        injected = self._injected("screen_count_mismatch")
        report = analyze_sample(injected)["report"]
        self.assertEqual("E_DIGEST", report["status"])
        self.assertIn("screen count", report["merge"]["digest_failures"][0]["reason"])

    def test_08_out_of_bounds_time_is_schema_without_report(self) -> None:
        injected = self._injected("time_out_of_bounds")
        report_path = injected / "must-not-exist-report.json"
        state_dir = injected / "must-not-exist-state"
        with self.assertRaises(SchemaError) as context:
            run_diagnostic(injected, report_path=report_path, state_dir=state_dir, install_signals=False)
        self.assertEqual("E_SCHEMA", context.exception.code)
        self.assertFalse(report_path.exists())
        self.assertFalse(state_dir.exists())

    def test_09_replay_drift_locates_first_field_mismatch(self) -> None:
        injected = self._injected("replay_drift")
        report = analyze_sample(injected)["report"]
        self.assertEqual("E_REPLAY", report["status"])
        mismatch = report["comparison"]["first_mismatch"]
        self.assertEqual(3000, mismatch["logic_ms"])
        self.assertEqual("canon:display:living-room-big", mismatch["canonical_screen_id"])
        self.assertEqual("keyframe_seq", mismatch["field"])
        self.assertEqual(5, mismatch["expected_replay_value"])
        self.assertEqual(4, mismatch["actual_merged_value"])

    def test_10_schema_validators(self) -> None:
        manifest_raw = json.loads((self.samples / "sessions.json").read_text(encoding="utf-8"))
        duplicated = copy.deepcopy(manifest_raw)
        duplicated["sessions"].append(copy.deepcopy(duplicated["sessions"][0]))
        with self.assertRaises(SchemaError):
            parse_manifest(duplicated)

        bad_geometry = copy.deepcopy(manifest_raw)
        bad_geometry["sessions"][0]["screens"][0]["width_px"] = 0
        with self.assertRaises(SchemaError):
            parse_manifest(bad_geometry)

        events_raw = json.loads((self.samples / "frame_events.json").read_text(encoding="utf-8"))
        bad_frame_order = copy.deepcopy(events_raw)
        bad_frame_order["events"][5]["frame_seq"] = 999
        manifest = parse_manifest(manifest_raw)
        with self.assertRaises(SchemaError):
            parse_events(bad_frame_order, manifest)

        bad_time = copy.deepcopy(events_raw)
        bad_time["events"][0]["sampled_at_ms"] = -1
        with self.assertRaises(SchemaError):
            parse_events(bad_time, manifest)

        self.assertEqual(1500, relative_seconds_to_ms(1.5, "x"))
        with self.assertRaises(SchemaError):
            relative_seconds_to_ms("1.2345", "x")

    def test_11_version_identity_depends_on_ordered_events(self) -> None:
        manifest_raw = json.loads((self.samples / "sessions.json").read_text(encoding="utf-8"))
        events_raw = json.loads((self.samples / "frame_events.json").read_text(encoding="utf-8"))["events"]
        first = version_identity(manifest_raw, events_raw)[0]
        reordered = list(events_raw)
        reordered[0], reordered[1] = reordered[1], reordered[0]
        second = version_identity(manifest_raw, reordered)[0]
        self.assertNotEqual(first, second)


class PersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(tempfile.mkdtemp(prefix="screen-diag-persist-"))
        cls.samples = generate_sample_tree(cls.root / "samples", seed=384)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, onerror=_on_rm_error)

    def test_12_interrupt_writes_marker_and_resume_completes(self) -> None:
        state_dir = self.root / "interrupt-state"
        first_report = self.root / "interrupted.json"
        exit_code, report = run_diagnostic(
            self.samples,
            report_path=first_report,
            state_dir=state_dir,
            interrupt_after_chunks=2,
            install_signals=False,
        )
        self.assertEqual(130, exit_code)
        self.assertEqual(130, report["exit_code"])
        self.assertTrue((state_dir / "INTERRUPTED.json").exists())
        self.assertEqual(2, report["recovery"]["chunk_count"])

        exit_code, report = run_diagnostic(
            self.samples,
            report_path=self.root / "resumed.json",
            state_dir=state_dir,
            resume=True,
            install_signals=False,
        )
        self.assertEqual(0, exit_code)
        self.assertFalse((state_dir / "INTERRUPTED.json").exists())
        self.assertEqual(4, report["recovery"]["chunk_count"])
        store = ChunkStore(state_dir, report["version_identity"]["version_id"])
        recovered = store.recover_latest()
        self.assertEqual(
            [2000, 4000, 6000, 8000],
            [chunk["boundary_logic_ms"] for chunk in recovered.chunks],
        )
        self.assertIn("merged_points", recovered.chunks[0]["body"])
        self.assertIn("merged_conflicts", recovered.chunks[0]["body"])
        self.assertIn("decisions", recovered.chunks[0]["body"])

    def test_13_corrupt_persisted_chunk_falls_back_and_resume_rebuilds(self) -> None:
        state_dir = self.root / "corrupt-chunk-state"
        exit_code, _report = run_diagnostic(
            self.samples,
            state_dir=state_dir,
            install_signals=False,
        )
        self.assertEqual(0, exit_code)

        chunk_path = state_dir / "chunks" / "chunk-0002.json"
        envelope = json.loads(chunk_path.read_text(encoding="utf-8"))
        envelope["payload"]["body"]["tampered_for_test"] = True
        chunk_path.write_text(json.dumps(envelope), encoding="utf-8")

        analysis = analyze_sample(self.samples)
        store = ChunkStore(state_dir, analysis["version_id"])
        recovered = store.recover_latest()
        self.assertEqual(2, recovered.chunk_count)
        self.assertEqual(2, recovered.invalid_chunk["chunk_index"])
        self.assertEqual(4000, recovered.last_boundary_logic_ms)

        exit_code, report = run_diagnostic(
            self.samples,
            state_dir=state_dir,
            resume=True,
            install_signals=False,
        )
        self.assertEqual(0, exit_code)
        self.assertIsNone(report["recovery"]["invalid_chunk"])
        self.assertEqual(4, report["recovery"]["chunk_count"])

    def test_14_signal_handler_sets_boundary_request(self) -> None:
        from screen_diag.persistence import InterruptionController

        controller = InterruptionController()
        controller.install_signal_handlers()
        try:
            controller._handle_signal(15, None)
            self.assertTrue(controller.stop_requested)
            self.assertTrue(controller.boundary_requested())
        finally:
            controller.restore_signal_handlers()


if __name__ == "__main__":
    unittest.main(verbosity=2)
