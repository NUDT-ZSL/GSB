from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tracker_app.engine import TrackingEngine
from tracker_app.errors import TrackingError
from tracker_app.jsonio import read_json, sha256_json
from tracker_app.matching import BoundedAppearanceCache, assign_frame
from tracker_app.models import Detection, Track
from tracker_app.storage import SequenceStore
from tracker_app.tracker import TrackerStateMachine

from tests.helpers import small_config
from tracker_app.data_gen import generate_dataset


def detection(truth_id: int, score: float, frame: int = 1) -> Detection:
    return Detection(
        seq_id="tie",
        revision=1,
        frame_index=frame,
        track_id_true=truth_id,
        x1=0.0,
        y1=0.0,
        x2=10.0,
        y2=10.0,
        score=score,
        class_id=0,
        occluded=False,
        appearance=(1.0, 0.0, 0.0),
    )


def track(track_id: int) -> Track:
    return Track(
        track_id=track_id,
        state="tracked",
        birth_frame=0,
        last_frame=0,
        last_bbox=(0.0, 0.0, 10.0, 10.0),
        appearance=(1.0, 0.0, 0.0),
        velocity=(0.0, 0.0, 0.0, 0.0),
    )


class MatchingStateTests(unittest.TestCase):
    def test_greedy_tie_break_uses_truth_id_then_score_descending(self) -> None:
        config = small_config()
        matching = assign_frame(
            seq_id="tie",
            revision=1,
            frame_index=1,
            detections=(detection(0, 0.80), detection(1, 0.95)),
            tracks={1: track(1), 2: track(2)},
            config=config,
            cache=BoundedAppearanceCache(10, "error"),
        )
        self.assertEqual(matching.detection_to_track, {0: 1, 1: 2})
        self.assertEqual(matching.candidates[0].track_id_true, 0)
        self.assertEqual(
            [candidate.cost for candidate in matching.candidates],
            sorted(candidate.cost for candidate in matching.candidates),
        )

    def test_lost_track_resurrects_with_same_id(self) -> None:
        config = small_config()
        old = track(7)
        machine = TrackerStateMachine(
            seq_id="tie",
            revision=1,
            config=config,
            tracks={7: old},
            next_track_id=8,
        )
        machine.process_frame((), 1)
        self.assertEqual(machine.tracks[7].state, "lost")
        result = machine.process_frame((detection(0, 0.9, frame=2),), 2)
        self.assertEqual(result.assignments[0]["pred_track_id"], 7)
        self.assertEqual(result.assignments[0]["track_event"], "resurrected")
        self.assertEqual(machine.tracks[7].state, "tracked")
        self.assertEqual(machine.next_track_id, 8)

    def test_terminated_track_cannot_resurrect(self) -> None:
        config = small_config()
        terminated = track(9)
        terminated.state = "terminated"
        terminated.termination_frame = 4
        machine = TrackerStateMachine(
            seq_id="tie",
            revision=1,
            config=config,
            tracks={9: terminated},
            next_track_id=10,
        )
        with self.assertRaises(TrackingError) as raised:
            machine._apply_hit(terminated, detection(0, 0.9, frame=5), 5)
        self.assertEqual(raised.exception.code, "E_TRACK_ILLEGAL")
        self.assertEqual(raised.exception.seq_id, "tie")
        self.assertEqual(raised.exception.frame_index, 5)

    def test_full_run_contains_loss_and_resurrection_and_correct_distribution(self) -> None:
        config = small_config()
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=Path(temp) / "runtime",
                output_dir=Path(temp) / "output",
            )
            result = engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            sequence = result["sequence_results"][0]
            events = {row["event"] for row in sequence.frame_view}
            self.assertIn("lost", events)
            self.assertIn("resurrected", events)
            first_frame = [row for row in sequence.frame_view if row["frame_index"] == 0]
            self.assertEqual(first_frame[0]["state_distribution"]["new"], 3)
            self.assertTrue(all(row["state_distribution"]["new"] == 3 for row in first_frame))

    def test_same_input_produces_identical_output_bytes(self) -> None:
        config = small_config()
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            outputs = []
            for name in ("first", "second"):
                engine = TrackingEngine(
                    config,
                    base_dir=temp,
                    runtime_dir=Path(temp) / f"runtime_{name}",
                    output_dir=Path(temp) / f"output_{name}",
                )
                result = engine.run_dataset(
                    dataset["detections"],
                    dataset["truth"],
                    dataset_manifest=dataset["manifest"],
                )
                outputs.append(result["sequence_results"][0].output_dir)
            for filename in (
                "assignments.jsonl",
                "match_candidates.jsonl",
                "track_states.jsonl",
                "segments.json",
                "metrics.json",
            ):
                self.assertEqual(
                    (outputs[0] / filename).read_bytes(),
                    (outputs[1] / filename).read_bytes(),
                    filename,
                )

    def test_checkpoint_chain_and_checksum_detection(self) -> None:
        config = small_config()
        dataset = generate_dataset(config)
        with tempfile.TemporaryDirectory() as temp:
            runtime = Path(temp) / "runtime"
            engine = TrackingEngine(
                config,
                base_dir=temp,
                runtime_dir=runtime,
                output_dir=Path(temp) / "output",
            )
            engine.run_dataset(
                dataset["detections"],
                dataset["truth"],
                dataset_manifest=dataset["manifest"],
            )
            store = SequenceStore(runtime, "seq-000")
            checkpoints = store.validate_revision_chain(1)
            self.assertEqual(
                [int(item["frame_index"]) for item in checkpoints],
                list(range(24)),
            )
            versions = [int(item["version"]) for item in checkpoints]
            self.assertEqual(versions, sorted(set(versions)))
            self.assertTrue(all(item["committed_complete"] for item in checkpoints))

            path = store.checkpoint_paths(1)[5]
            original = read_json(path)
            tampered = dict(original)
            tampered["frame_index"] = int(tampered["frame_index"]) + 1
            path.write_text(
                __import__("json").dumps(tampered, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            with self.assertRaises(TrackingError) as raised:
                store.read_checkpoint(path)
            self.assertEqual(raised.exception.code, "E_CHECKPOINT_CORRUPT")


if __name__ == "__main__":
    unittest.main()
