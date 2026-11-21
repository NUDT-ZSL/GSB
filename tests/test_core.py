from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mute_attribution.attribution import compute_attribution_record
from mute_attribution.config import load_config
from mute_attribution.consistency import run_consistency
from mute_attribution.errors import BusinessError
from mute_attribution.features import (
    compute_feature_record,
    feature_input_fingerprint,
    load_manifest,
)
from mute_attribution.lineage import replay_event
from mute_attribution.noise import compute_noise_record
from mute_attribution.pipeline import Pipeline
from mute_attribution.profile import derive_events_from_attribution, validate_events
from mute_attribution.store import SnapshotStore
from mute_attribution.synth import (
    edit_truth,
    generate_dataset,
    replace_pcm_gain,
)
from mute_attribution.util import canonical_json, read_json, read_jsonl, write_jsonl


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.data = self.root / "data"
        generate_dataset(self.data, seed=20261122, calls=3)
        self.config = load_config(PROJECT_ROOT / "configs" / "default.json")
        self.calls = load_manifest(self.data)
        self.call_map = {call.call_id: call for call in self.calls}

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def assert_business_code(self, code: str, action: Any) -> BusinessError:
        with self.assertRaises(BusinessError) as context:
            action()
        self.assertEqual(context.exception.code, code)
        return context.exception

    def build_records(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        call = self.call_map["call_0001"]
        feature = compute_feature_record(call, self.data, self.config, 1)
        noise = compute_noise_record(
            feature, self.config["noise_floor"], self.config, 1
        )
        truth = read_json(self.data / "truth" / "call_0001.json")
        attr = compute_attribution_record(
            call, feature, noise, truth, self.config, 1, "test-revision"
        )
        return feature, noise, attr

    def test_features_are_deterministic_and_frame_aligned(self) -> None:
        call = self.call_map["call_0001"]
        first = compute_feature_record(call, self.data, self.config, 1)
        second = compute_feature_record(call, self.data, self.config, 1)
        self.assertEqual(canonical_json(first), canonical_json(second))
        observation_count = len(
            read_jsonl(self.data / "observations" / "call_0001.jsonl")
        )
        self.assertEqual(first["frame_count"], observation_count)
        self.assertEqual(first["frames"][0]["sample_start"], 0)
        self.assertEqual(first["frames"][0]["sample_end"], 320)
        self.assertEqual(first["frames"][-1]["sample_end"], 240 * 320)

    def test_frame_mismatch_reports_call_and_frame(self) -> None:
        call = self.call_map["call_0001"]
        path = self.data / "observations" / "call_0001.jsonl"
        observations = read_jsonl(path)
        write_jsonl(path, observations + [observations[-1]])
        error = self.assert_business_code(
            "E_FRAME_MISMATCH",
            lambda: compute_feature_record(call, self.data, self.config, 1),
        )
        self.assertEqual(error.context["call_id"], "call_0001")
        self.assertEqual(error.context["mismatch_frame"], 240)

    def test_noise_window_and_step_errors(self) -> None:
        feature, _, _ = self.build_records()
        window_error = self.assert_business_code(
            "E_WINDOW_INVALID",
            lambda: compute_noise_record(
                feature,
                {
                    **self.config["noise_floor"],
                    "window_frames": feature["frame_count"] + 1,
                    "step_frames": 5,
                },
                self.config,
                2,
            ),
        )
        self.assertEqual(window_error.context["call_id"], "call_0001")
        step_error = self.assert_business_code(
            "E_STEP_INVALID",
            lambda: compute_noise_record(
                feature,
                {
                    **self.config["noise_floor"],
                    "window_frames": 25,
                    "step_frames": 0,
                },
                self.config,
                2,
            ),
        )
        self.assertEqual(step_error.context["call_id"], "call_0001")

    def test_attribution_labels_and_segment_errors(self) -> None:
        feature, noise, attr = self.build_records()
        counts = {
            label: sum(
                1
                for segment in attr["silence_segments"]
                if segment["label"] == label
            )
            for label in self.config["attribution"]["controlled_labels"]
        }
        self.assertEqual(
            counts,
            {
                "stationary_floor": 3,
                "device_step": 1,
                "crosstalk_residue": 1,
            },
        )
        call = self.call_map["call_0001"]
        truth = read_json(self.data / "truth" / "call_0001.json")
        label_error = self.assert_business_code(
            "E_LABEL_INVALID",
            lambda: compute_attribution_record(
                call,
                feature,
                noise,
                truth,
                self.config,
                2,
                "test",
                forced_label="bad_label",
            ),
        )
        self.assertEqual(label_error.context["call_id"], "call_0001")
        bad_truth = dict(truth)
        bad_truth["frame_count"] = 236
        split_error = self.assert_business_code(
            "E_SEGMENT_SPLIT",
            lambda: compute_attribution_record(
                call, feature, noise, bad_truth, self.config, 2, "test"
            ),
        )
        self.assertEqual(split_error.context["feature_frames"], 240)
        self.assertEqual(split_error.context["truth_frames"], 236)

    def test_profile_range_and_overlap_errors(self) -> None:
        call = self.call_map["call_0001"]
        range_error = self.assert_business_code(
            "E_EVENT_RANGE",
            lambda: validate_events(
                [
                    {
                        "event_id": "call_0001-E999",
                        "start_frame": 0,
                        "end_frame": 999,
                    }
                ],
                call,
            ),
        )
        self.assertEqual(range_error.context["event_id"], "call_0001-E999")
        overlap_error = self.assert_business_code(
            "E_EVENT_OVERLAP",
            lambda: validate_events(
                [
                    {
                        "event_id": "call_0001-E001",
                        "start_frame": 0,
                        "end_frame": 10,
                    },
                    {
                        "event_id": "call_0001-E002",
                        "start_frame": 5,
                        "end_frame": 12,
                    },
                ],
                call,
            ),
        )
        self.assertEqual(overlap_error.context["event_id"], "call_0001-E002")

    def test_profile_merges_same_label_segments_with_allowed_gap(self) -> None:
        call = self.call_map["call_0001"]
        attr = {
            "silence_segments": [
                {
                    "segment_id": "call_0001-S001",
                    "start_frame": 0,
                    "end_frame": 10,
                    "label": "stationary_floor",
                    "metrics": {"deviation_db": 0.2, "volatility": 0.1},
                    "lineage": {"snapshot_version": 1},
                },
                {
                    "segment_id": "call_0001-S002",
                    "start_frame": 13,
                    "end_frame": 20,
                    "label": "stationary_floor",
                    "metrics": {"deviation_db": 0.4, "volatility": 0.2},
                    "lineage": {"snapshot_version": 1},
                },
            ],
            "speech_ranges": [],
        }
        events = derive_events_from_attribution(
            call, attr, {"min_gap_frames": 5}
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["start_frame"], 0)
        self.assertEqual(events[0]["end_frame"], 20)

    def test_pipeline_reuses_and_replays_lineage(self) -> None:
        store = SnapshotStore(self.root / "store")
        run = Pipeline(self.data, store, self.config).run()
        self.assertEqual(run["code"], "OK")
        rerun = Pipeline(self.data, store, self.config).run()
        self.assertTrue(
            all(stage["status"] == "reused" for stage in rerun["stages"])
        )
        profile = store.get_current("profile")
        assert profile is not None
        event = profile.records[0]["events"][0]
        replay = replay_event(store, event, self.call_map[event["call_id"]])
        self.assertEqual(replay["label"], event["label"])
        self.assertEqual(replay["deviation_db"], event["deviation_db"])
        consistency = run_consistency(store, self.calls)
        self.assertEqual(consistency["code"], "OK")

    def test_config_bump_replaces_snapshot_and_archives_old(self) -> None:
        store = SnapshotStore(self.root / "store")
        first = Pipeline(self.data, store, self.config).run()
        bumped = json.loads(canonical_json(self.config))
        bumped["noise_floor"]["version"] = 2
        second = Pipeline(self.data, store, bumped).run()
        self.assertEqual(second["versions"]["features"], 1)
        self.assertEqual(second["versions"]["noise_floor"], 2)
        self.assertEqual(second["versions"]["attribution"], 2)
        self.assertEqual(second["versions"]["profile"], 2)
        archived = {
            path.parent.name
            for path in (self.root / "store" / "archive").glob("*/v*/snapshot.json")
        }
        self.assertEqual(archived, {"v000001"})
        old_feature = (
            self.root
            / "store"
            / "stages"
            / "features"
            / "versions"
            / "v000001"
            / "snapshot.json"
        )
        self.assertTrue(old_feature.exists())
        self.assertEqual(first["versions"]["features"], 1)

    def test_truth_edit_recomputes_only_affected_call_suffix(self) -> None:
        store = SnapshotStore(self.root / "store")
        Pipeline(self.data, store, self.config).run()
        edit_truth(self.data, "call_0001", 216, 228, "speech")
        updated = Pipeline(self.data, store, self.config).run()
        self.assertEqual(
            next(
                stage
                for stage in updated["stages"]
                if stage["stage"] == "features"
            )["status"],
            "reused",
        )
        self.assertEqual(
            next(
                stage
                for stage in updated["stages"]
                if stage["stage"] == "noise_floor"
            )["status"],
            "reused",
        )
        attr = next(
            stage for stage in updated["stages"] if stage["stage"] == "attribution"
        )
        self.assertEqual(attr["recomputed_calls"], ["call_0001"])
        self.assertEqual(attr["affected_frame_ranges"], {"call_0001": [[220, 228]]})

    def test_pcm_and_observation_replacement_targets_one_call(self) -> None:
        store = SnapshotStore(self.root / "store")
        Pipeline(self.data, store, self.config).run()
        replace_pcm_gain(self.data, "call_0002", 1.1)
        updated = Pipeline(self.data, store, self.config).run()
        for stage in updated["stages"]:
            self.assertEqual(stage["recomputed_calls"], ["call_0002"])
        self.assertEqual(run_consistency(store, self.calls)["code"], "OK")

    def test_snapshot_corruption_and_rollback_errors(self) -> None:
        store = SnapshotStore(self.root / "store")
        run = Pipeline(self.data, store, self.config).run()
        rollback = self.assert_business_code(
            "E_SNAPSHOT_ROLLBACK",
            lambda: store.attempt_rollback(
                "features", run["versions"]["features"]
            ),
        )
        self.assertEqual(rollback.context["stage"], "features")
        snapshot_path = (
            self.root
            / "store"
            / "stages"
            / "features"
            / "versions"
            / "v000001"
            / "snapshot.json"
        )
        payload = read_json(snapshot_path)
        payload["tampered"] = True
        snapshot_path.write_text(canonical_json(payload), encoding="utf-8")
        corrupt = self.assert_business_code(
            "E_SNAPSHOT_CORRUPT", lambda: store.get_current("features")
        )
        self.assertEqual(corrupt.context["stage"], "features")

    def test_forced_termination_recovery_is_byte_identical(self) -> None:
        baseline_store = SnapshotStore(self.root / "baseline_store")
        baseline = Pipeline(self.data, baseline_store, self.config).run()
        crash_store = self.root / "crash_store"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "mute_attribution",
                "run",
                "--data-dir",
                str(self.data),
                "--store",
                str(crash_store),
                "--config",
                str(PROJECT_ROOT / "configs" / "default.json"),
                "--crash-at",
                "noise_floor:1",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 137)
        state = read_json(crash_store / "state.json")
        self.assertEqual(list(state["stages"]), ["features"])
        recovered = Pipeline(
            self.data, SnapshotStore(crash_store), self.config
        ).run()
        self.assertEqual(recovered["recovery"]["resumed_from"], "features")
        for stage, version in baseline["versions"].items():
            baseline_bytes = (
                self.root
                / "baseline_store"
                / "stages"
                / stage
                / "versions"
                / f"v{version:06d}"
                / "snapshot.json"
            ).read_bytes()
            recovered_bytes = (
                crash_store
                / "stages"
                / stage
                / "versions"
                / f"v{version:06d}"
                / "snapshot.json"
            ).read_bytes()
            self.assertEqual(baseline_bytes, recovered_bytes)
        self.assertEqual(
            sum(1 for _ in (crash_store / "work").rglob("*")), 0
        )


if __name__ == "__main__":
    unittest.main()

