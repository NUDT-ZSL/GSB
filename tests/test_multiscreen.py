from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from multiscreen.canon import canonical
from multiscreen.concurrency import OrderedMemoryGate
from multiscreen.digest import select_snapshot_set, verify_snapshot_set
from multiscreen.engine import replay_receiver
from multiscreen.errors import ScreenError
from multiscreen.generator import generate_sample
from multiscreen.persist import recover_chunks, version_id_of
from multiscreen.pipeline import InterruptFlag, prepare_data, run_pipeline
from multiscreen.replay import run_replay
from multiscreen.validate import load_manifest, normalize_events
from multiscreen.verify import build_manifest_initial_states, verify_replay


class SampleWorkspace:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "samples"
        self.state = self.root / "state"
        generate_sample(self.data)

    def cleanup(self) -> None:
        self.temp.cleanup()

    def copy_data(self, name: str = "copy") -> Path:
        target = self.root / name
        shutil.copytree(self.data, target)
        return target


class EndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = SampleWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_generated_sample_passes_and_persists(self) -> None:
        report = run_pipeline(
            self.ws.data,
            self.ws.state,
            workers=4,
            budget_bytes=64 * 1024 * 1024,
            chunk_count=4,
        )
        self.assertTrue(report["ok"], report.get("verification", {}).get("divergences"))
        self.assertEqual(2, report["verification"]["stats"]["rebuild_reports"])
        self.assertEqual(4, report["persistence"]["chunks_recovered"])
        self.assertTrue(report["persistence"]["chunk_state_matches_full_replay"])
        self.assertEqual(8, len(report["merge"]["conflicts"]))
        self.assertTrue((self.ws.state / "metadata.sqlite3").is_file())
        self.assertTrue((self.ws.state / "report.json").is_file())

    def test_rebuild_report_has_required_cursor_fields(self) -> None:
        prep = prepare_data(self.ws.data)
        initial = build_manifest_initial_states(prep["manifest"])
        replay = run_replay(prep["events"], initial, workers=1, auto_switch_on_breach=True)
        by_receiver = {item["receiver_id"]: item for item in replay["rebuilds"]}

        side = next(item for item in replay["rebuilds"] if item["receiver_id"] == "rcv-side")
        self.assertEqual("explicit_generation_marker", side["trigger_reason"])
        self.assertEqual(8, side["old_cursor"]["adoption_cursor_seq"])
        self.assertEqual(6, side["recovery_keyframe"]["frame_seq"])
        self.assertEqual(11, side["new_baseline"]["frame_seq"])
        self.assertEqual([7, 8], [item["frame_seq"] for item in side["discarded_increments"]])
        self.assertEqual([9, 10], [item["frame_seq"] for item in side["post_switch_discarded_increments"]])

        projector = by_receiver["rcv-projector"]
        self.assertEqual("tolerance_window_exceeded", projector["trigger_reason"])
        self.assertEqual(3, projector["old_cursor"]["adoption_cursor_seq"])
        self.assertEqual(10, projector["new_baseline"]["frame_seq"])
        self.assertIn(5, [item["frame_seq"] for item in projector["discarded_increments"]])

    def test_concurrency_degree_does_not_change_result(self) -> None:
        prep = prepare_data(self.ws.data, workers=1)
        initial = build_manifest_initial_states(prep["manifest"])
        serial = run_replay(prep["events"], initial, workers=1, auto_switch_on_breach=True)
        concurrent = run_replay(prep["events"], initial, workers=3, budget_bytes=1024 * 1024,
                                auto_switch_on_breach=True)
        self.assertEqual(canonical(serial["results"]), canonical(concurrent["results"]))
        self.assertEqual(canonical(serial["trace"]), canonical(concurrent["trace"]))
        self.assertEqual(canonical(serial["rebuilds"]), canonical(concurrent["rebuilds"]))

        report1 = verify_replay(prep["view"], prep["events"], prep["manifest"], prep["records"], workers=1)
        report3 = verify_replay(prep["view"], prep["events"], prep["manifest"], prep["records"], workers=3,
                                budget_bytes=1024 * 1024)
        self.assertTrue(report1["ok"])
        self.assertEqual(
            canonical(report1["stats"]),
            canonical(report3["stats"]),
        )

    def test_gap_collects_multiple_sequence_jumps(self) -> None:
        events = [
            {"event_seq": 1, "record_type": "frame", "receiver_id": "r", "frame_seq": 1,
             "sample_ms": 0, "generation": 1, "frame_type": "key"},
            {"event_seq": 2, "record_type": "frame", "receiver_id": "r", "frame_seq": 2,
             "sample_ms": 33, "generation": 1, "frame_type": "delta"},
            {"event_seq": 3, "record_type": "frame", "receiver_id": "r", "frame_seq": 5,
             "sample_ms": 132, "generation": 1, "frame_type": "delta"},
            {"event_seq": 4, "record_type": "frame", "receiver_id": "r", "frame_seq": 8,
             "sample_ms": 231, "generation": 1, "frame_type": "delta"},
        ]
        result = replay_receiver("r", events)
        gap = result["state"]["open_gap"]
        self.assertEqual([3, 4, 6, 7], gap["missing_frame_seqs"])
        self.assertEqual([5, 8], gap["rejected_frame_seqs"])


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = SampleWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_invalid_geometry_returns_business_code_and_no_state(self) -> None:
        data = self.ws.copy_data("bad-geometry")
        manifest_path = data / "sessions.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["sessions"][0]["geometry"]["width_px"] = -1
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        state = self.ws.root / "should-not-exist"

        from multiscreen.cli import main

        with self.assertRaises(ScreenError) as caught:
            prepare_data(data)
        self.assertEqual("E_GEOMETRY", caught.exception.code)
        exit_code = main(["run", "--data", str(data), "--state", str(state)])
        self.assertEqual(1, exit_code)
        self.assertFalse(state.exists())

    def test_frame_order_clock_and_type_validation(self) -> None:
        manifest = load_manifest(self.ws.data / "sessions.json")
        base_event = {
            "event_seq": 1,
            "session_id": "sess-room-aux",
            "receiver_id": "rcv-panel",
            "frame_seq": 1,
            "sample_ms": 0,
            "frame_type": "key",
            "keyframe": True,
        }
        with self.assertRaises(ScreenError) as caught:
            normalize_events(
                [
                    {**base_event, "event_seq": 1, "frame_seq": 2},
                    {**base_event, "event_seq": 2, "frame_seq": 1},
                ],
                manifest,
            )
        self.assertEqual("E_FRAME_ORDER", caught.exception.code)

        with self.assertRaises(ScreenError) as caught:
            normalize_events([{**base_event, "sample_ms": -1}], manifest)
        self.assertEqual("E_CLOCK", caught.exception.code)

        with self.assertRaises(ScreenError) as caught:
            normalize_events([{**base_event, "frame_type": "delta", "keyframe": True}], manifest)
        self.assertEqual("E_FRAME_TYPE", caught.exception.code)


class MergeDigestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = SampleWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_v1_seconds_convert_and_tombstone_wins(self) -> None:
        prep = prepare_data(self.ws.data)
        side = prep["view"]["rcv-side"]
        panel = prep["view"]["rcv-panel"]
        self.assertEqual(1231, next(r for r in prep["records"] if r["source_set_id"] == "v1"
                                    and r["canonical_receiver_id"] == "rcv-side")["logical_ts_ms"])
        self.assertTrue(panel["tombstone"])
        self.assertEqual("v2/shard_002.json", panel["source_shard"])
        self.assertTrue(all(c["winner"]["source_shard"] for c in prep["conflicts"]))
        self.assertTrue(any(c["rule"] == "tombstone_priority" for c in prep["conflicts"]))

    def test_tampered_current_shard_falls_back_to_valid_backup(self) -> None:
        data = self.ws.copy_data("tampered-current")
        shard = data / "snapshots" / "v2" / "shard_000.json"
        shard.write_bytes(shard.read_bytes() + b" ")
        prep = prepare_data(data, workers=2)
        self.assertEqual("v2-backup", prep["selected_v2_set_id"])
        self.assertEqual("v2-backup", prep["fallback"]["to_set_id"])
        self.assertTrue(any(item["code"] == "E_DIGEST" for item in prep["rejected_snapshots"]))
        report = run_pipeline(data, self.ws.state, workers=2, chunk_count=3)
        self.assertTrue(report["ok"])

    def test_declared_count_mismatch_is_digest_failure(self) -> None:
        data = self.ws.copy_data("bad-count")
        digest_path = data / "snapshots" / "digests.json"
        payload = json.loads(digest_path.read_text(encoding="utf-8"))
        current = next(item for item in payload["snapshot_sets"] if item["set_id"] == "v2-current")
        current["files"][0]["declared_receiver_count"] = 999
        digest_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(ScreenError) as caught:
            verify_snapshot_set(data / "snapshots", current)
        self.assertEqual("E_DIGEST", caught.exception.code)

    def test_all_snapshots_invalid_returns_digest_error(self) -> None:
        data = self.ws.copy_data("all-bad")
        for relative in ("snapshots/v2/shard_000.json", "snapshots/v2-backup/shard_000.json",
                         "snapshots/v1/snapshot_v1.json"):
            path = data / relative
            path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaises(ScreenError) as caught:
            prepare_data(data)
        self.assertEqual("E_DIGEST", caught.exception.code)


class ReplayAndPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = SampleWorkspace()

    def tearDown(self) -> None:
        self.ws.cleanup()

    def test_replay_divergence_locates_receiver_and_field(self) -> None:
        prep = prepare_data(self.ws.data)
        view = {key: dict(value) for key, value in prep["view"].items()}
        view["rcv-side"]["adoption_cursor_seq"] = 999
        report = verify_replay(view, prep["events"], prep["manifest"], prep["records"], workers=1)
        self.assertFalse(report["ok"])
        self.assertEqual("E_REPLAY", report["code"])
        first = report["first_divergence"]
        self.assertEqual("rcv-side", first["receiver_id"])
        self.assertEqual("adoption_cursor_seq", first["field"])
        self.assertIsNotNone(first["logical_ms"])

    def test_interrupt_and_resume(self) -> None:
        interrupted = run_pipeline(
            self.ws.data,
            self.ws.state,
            workers=2,
            chunk_count=3,
            interrupt_after_chunks=1,
        )
        self.assertTrue(interrupted["interrupted"])
        self.assertEqual(130, interrupted["exit_code"])
        version = interrupted["version_id"]
        recovery = recover_chunks(self.ws.state, version)
        self.assertEqual(1, recovery["chunk_count"])
        self.assertTrue((self.ws.state / "_interrupted.json").is_file())

        report = run_pipeline(self.ws.data, self.ws.state, workers=2, chunk_count=3)
        self.assertTrue(report["ok"])
        self.assertEqual(1, report["persistence"]["resumed_from_chunk"])
        self.assertEqual(3, report["persistence"]["chunks_recovered"])
        self.assertFalse((self.ws.state / "_interrupted.json").exists())

    def test_signal_flag_takes_effect_only_at_chunk_boundary(self) -> None:
        flag = InterruptFlag()
        flag.trigger("unit-test-signal")
        result = run_pipeline(self.ws.data, self.ws.state, chunk_count=5, interrupt_flag=flag)
        self.assertTrue(result["interrupted"])
        self.assertEqual(0, result["chunks_written"])
        self.assertEqual(0, recover_chunks(self.ws.state, result["version_id"])["chunk_count"])

    def test_recover_only_valid_prefix_after_forced_partial_write(self) -> None:
        run_pipeline(self.ws.data, self.ws.state, chunk_count=3, interrupt_after_chunks=1)
        version = version_id_of(
            load_manifest(self.ws.data / "sessions.json"),
            normalize_events(self.ws.data / "frames.jsonl", load_manifest(self.ws.data / "sessions.json")),
        )
        bad = self.ws.state / "chunk_00001.json"
        bad.write_text('{"format":"multiscreen.chunk","digest":"broken"', encoding="utf-8")
        (self.ws.state / "chunk_00002.json").write_text(
            json.dumps({"format": "multiscreen.chunk", "index": 2, "body": {"x": 1}}), encoding="utf-8"
        )
        recovery = recover_chunks(self.ws.state, version)
        self.assertEqual(1, recovery["chunk_count"])
        self.assertEqual("unparseable_chunk", recovery["rejected"][0]["reason"])

    def test_ordered_budget_gate_waits_in_key_order(self) -> None:
        gate = OrderedMemoryGate(10, ["a", "b", "c"])
        order: list[str] = []
        gate.acquire("a", 6)

        def wait_for(key: str, size: int) -> None:
            gate.acquire(key, size)
            order.append(key)

        thread_b = threading.Thread(target=wait_for, args=("b", 6))
        thread_c = threading.Thread(target=wait_for, args=("c", 1))
        thread_b.start()
        thread_c.start()
        gate.release("a", 6)
        thread_b.join(timeout=2)
        gate.release("b", 6)
        thread_c.join(timeout=2)
        self.assertFalse(thread_b.is_alive())
        self.assertFalse(thread_c.is_alive())
        self.assertEqual(["b", "c"], order)

    def test_single_buffer_over_budget_returns_business_error(self) -> None:
        prep = prepare_data(self.ws.data)
        initial = build_manifest_initial_states(prep["manifest"])
        with self.assertRaises(ScreenError) as caught:
            run_replay(prep["events"], initial, workers=1, budget_bytes=16)
        self.assertEqual("E_BUDGET", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
