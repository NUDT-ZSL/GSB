from __future__ import annotations

import copy
import json
import math
import shutil
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from weld_train.generator import (
    DEFAULT_SEED,
    build_project,
    build_segment,
    generate_project,
)
from weld_train.geometry import DEFAULT_SPEAKER_LAYOUT
from weld_train.service import create_app
from weld_train.snapshots import (
    SnapshotReplaced,
    SnapshotStore,
)
from weld_train.vbap import panning_gains, virtual_azimuth_from_gains


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def setUpModule() -> None:
    if not (DATA / "validation" / "records.json").exists():
        generate_project(DATA, seed=DEFAULT_SEED)


def angular_error(first: float, second: float) -> float:
    return abs((first - second + 180.0) % 360.0 - 180.0)


class VbapTests(unittest.TestCase):
    def test_ring_covers_front_and_rear_with_constant_power(self):
        for azimuth in np.linspace(0.0, 359.0, 361):
            gains = panning_gains(float(azimuth), DEFAULT_SPEAKER_LAYOUT)
            self.assertAlmostEqual(float(np.sum(gains * gains)), 1.0, places=12)
            reconstructed = virtual_azimuth_from_gains(
                gains, DEFAULT_SPEAKER_LAYOUT
            )
            self.assertLessEqual(
                angular_error(reconstructed, float(azimuth)), 1e-8
            )

        front = panning_gains(0.0, DEFAULT_SPEAKER_LAYOUT)
        rear = panning_gains(180.0, DEFAULT_SPEAKER_LAYOUT)
        self.assertAlmostEqual(float(front[0]), 1.0, places=12)
        self.assertAlmostEqual(float(rear[4]), 1.0, places=12)
        self.assertTrue(np.all(front[[1, 2, 3, 4, 5, 6, 7]] == 0.0))
        self.assertTrue(np.all(rear[[0, 1, 2, 3, 5, 6, 7]] == 0.0))


class DeterminismTests(unittest.TestCase):
    def test_fixed_seed_generates_identical_boundary_inputs(self):
        project_a = build_project(DEFAULT_SEED)
        project_b = build_project(DEFAULT_SEED)
        self.assertEqual(project_a, project_b)
        for boundary_type in [
            "listener_facing_source",
            "listener_back_to_source",
            "dense_spatter",
            "pool_size_surge",
        ]:
            first = build_segment(project_a, boundary_type)
            second = build_segment(project_b, boundary_type)
            self.assertEqual(first["source_digest"], second["source_digest"])
            self.assertEqual(first, second)


class SnapshotTests(unittest.TestCase):
    def test_replacement_is_immutable_and_old_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SnapshotStore(
                Path(directory) / "snapshots",
                DEFAULT_SPEAKER_LAYOUT,
            )
            first_payload = {
                "schema_version": 1,
                "segment": {"segment_id": "boundary_front"},
                "speaker_layout": DEFAULT_SPEAKER_LAYOUT,
                "field": {"value": "first"},
                "audio": {"sha256": "a"},
            }
            first_version, first_record, _ = store.save(
                "boundary_front", first_payload
            )
            first_path = (
                Path(directory)
                / "snapshots"
                / "boundary_front"
                / first_record["path"]
            )
            first_bytes = first_path.read_bytes()

            second_payload = copy.deepcopy(first_payload)
            second_payload["field"]["value"] = "second"
            second_version, second_record, replaced = store.save(
                "boundary_front", second_payload
            )
            self.assertEqual(replaced, first_version)
            self.assertNotEqual(first_version, second_version)
            self.assertEqual(first_path.read_bytes(), first_bytes)

            active = store.load("boundary_front", second_version)
            self.assertEqual(
                active["field"]["value"],
                "second",
            )
            with self.assertRaises(SnapshotReplaced):
                store.load("boundary_front", first_version)

    def test_flask_returns_business_codes_and_stays_available(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_data = Path(directory) / "data"
            shutil.copytree(DATA, temp_data)
            project = json.loads(
                (temp_data / "project.json").read_text(encoding="utf-8")
            )
            manifest = json.loads(
                (
                    temp_data
                    / "snapshots"
                    / "boundary_front"
                    / "manifest.json"
                ).read_text(encoding="utf-8")
            )
            first_version = manifest["active_version"]
            first_snapshot = json.loads(
                (
                    temp_data
                    / "snapshots"
                    / "boundary_front"
                    / "versions"
                    / f"{first_version}.json"
                ).read_text(encoding="utf-8")
            )
            first_snapshot.pop("snapshot_version", None)
            first_snapshot.pop("content_sha256", None)
            first_snapshot["field"]["frames"][0]["state"] = "replaced_demo"
            replacement_store = SnapshotStore(
                temp_data / "snapshots", project["speaker_layout"]
            )
            replacement_version, _, replaced = replacement_store.save(
                "boundary_front", first_snapshot
            )
            self.assertEqual(replaced, first_version)

            app = create_app(temp_data)
            client = app.test_client()

            missing = client.get(
                "/api/segments/boundary_front/replay"
            )
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(
                missing.get_json()["error"]["business_code"], 2001
            )

            unknown = client.get(
                "/api/segments/boundary_front/replay?version=v9999"
            )
            self.assertEqual(unknown.status_code, 404)
            self.assertEqual(
                unknown.get_json()["error"]["business_code"], 2002
            )

            replaced_response = client.get(
                f"/api/segments/boundary_front/replay?version={first_version}"
            )
            self.assertEqual(replaced_response.status_code, 409)
            self.assertEqual(
                replaced_response.get_json()["error"]["business_code"], 2003
            )
            active_response = client.get(
                f"/api/segments/boundary_front/replay?version={replacement_version}"
            )
            self.assertEqual(active_response.status_code, 200)

            invalid_version = client.get(
                "/api/segments/boundary_front/replay?version=not-a-version"
            )
            self.assertEqual(invalid_version.status_code, 400)
            self.assertEqual(
                invalid_version.get_json()["error"]["business_code"], 2004
            )

            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertTrue(health.get_json()["ok"])


class GeneratedArtifactTests(unittest.TestCase):
    def test_generated_validation_passes(self):
        record = json.loads(
            (DATA / "validation" / "records.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(record["pass"])
        self.assertEqual(len(record["segments"]), 4)
        for segment in record["segments"]:
            self.assertTrue(segment["pass"])
            self.assertLessEqual(
                segment["checks"]["geometry_distance"]["distance"][
                    "max_abs_error_m"
                ],
                record["tolerances"]["distance_m"],
            )
            self.assertLessEqual(
                segment["checks"]["geometry_distance"]["panning"][
                    "global_relative_energy_error"
                ],
                record["tolerances"]["global_relative_energy"],
            )
            report = json.loads(
                (
                    DATA / "outputs" / segment["segment_id"] / "report.json"
                ).read_text(encoding="utf-8")
            )
            self.assertTrue(
                report["audibility_assessment"]["distinguishable"]
            )

    def test_realtime_and_replay_are_identical(self):
        record = json.loads(
            (DATA / "outputs" / "boundary_spatter_dense" / "consistency.json")
            .read_text(encoding="utf-8")
        )
        self.assertTrue(record["pass"])
        self.assertEqual(record["frame_count"], 160)
        for name, value in record["max_abs_differences"].items():
            tolerance = (
                record["tolerances"]["panning_gain_abs"]
                if "gain" in name or "rms" in name
                else record["tolerances"]["azimuth_deg"]
                if "azimuth" in name
                else record["tolerances"]["elevation_deg"]
                if "elevation" in name
                else record["tolerances"]["distance_m"]
            )
            self.assertLessEqual(value, tolerance)

    def test_api_replay_report_audio_and_versions(self):
        app = create_app(DATA)
        client = app.test_client()
        segments = client.get("/api/segments").get_json()["segments"]
        self.assertEqual(len(segments), 4)

        version = segments[0]["active_snapshot"]["version"]
        segment_id = segments[0]["segment_id"]
        replay = client.get(
            f"/api/segments/{segment_id}/replay?version={version}"
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["replay_verification"]["pass"])

        report = client.get(
            f"/api/segments/{segment_id}/report?version={version}"
        )
        self.assertEqual(report.status_code, 200)
        report_body = report.get_json()["report"]
        self.assertIn("listening_signature", report_body)
        self.assertIn("recompute", report_body)

        audio = client.get(
            f"/api/snapshots/{segment_id}/{version}/audio"
        )
        self.assertEqual(audio.status_code, 200)
        self.assertEqual(audio.mimetype, "audio/wav")
        self.assertGreater(len(audio.data), 1000)
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            handle.write(audio.data)
            handle.flush()
            with wave.open(handle.name, "rb") as wav_file:
                self.assertEqual(wav_file.getnchannels(), 8)
                self.assertEqual(wav_file.getframerate(), 16000)
                self.assertEqual(wav_file.getsampwidth(), 2)
                self.assertEqual(wav_file.getnframes(), 128000)


if __name__ == "__main__":
    unittest.main()
