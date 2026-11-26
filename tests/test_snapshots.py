from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ifdata.engine import PipelineEngine
from ifdata.errors import (
    BusinessError,
    E_INVALID_INPUT,
    E_SNAPSHOT_CORRUPT,
    ForcedTermination,
)
from ifdata.generator import generate_fixture
from ifdata.state import StageSnapshotStore
from ifdata.utils import read_json


class SnapshotTests(unittest.TestCase):
    def test_atomic_chain_requires_all_samples_and_valid_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            store = StageSnapshotStore(temp)
            first = store.commit(
                stage="ingest",
                payload={"value": 1},
                completed_sample_ids=["s2", "s1"],
                expected_sample_ids=["s1", "s2"],
            )
            second = store.commit(
                stage="template",
                payload={"value": 2},
                completed_sample_ids=["s1", "s2"],
                expected_sample_ids=["s1", "s2"],
            )
            self.assertEqual(first["snapshot_version"], 1)
            self.assertEqual(second["snapshot_version"], 2)
            self.assertEqual(
                second["parent_checksum"], first["checksum"]
            )
            with self.assertRaises(BusinessError) as context:
                store.commit(
                    stage="quality",
                    payload={},
                    completed_sample_ids=["s1"],
                    expected_sample_ids=["s1", "s2"],
                )
            self.assertEqual(context.exception.code, E_INVALID_INPUT)
            snapshot_path = Path(temp) / "stages/02-template.json"
            data = read_json(snapshot_path)
            data["payload"]["value"] = 999
            snapshot_path.write_text(
                __import__("json").dumps(data), encoding="utf-8"
            )
            with self.assertRaises(BusinessError) as context:
                store.load_snapshots()
            self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)

    def test_abandoned_temp_state_is_discarded(self):
        with tempfile.TemporaryDirectory() as temp:
            orphan = Path(temp) / "stages" / ".tmp-orphan"
            orphan.parent.mkdir(parents=True)
            orphan.write_text("partial", encoding="utf-8")
            store = StageSnapshotStore(temp)
            self.assertFalse(orphan.exists())
            self.assertEqual(store.removed_temp_files, [str(orphan)])

    def test_engine_resumes_from_next_stage_without_recompute(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=20)
            engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state",
                out_dir=root / "out",
            )
            with self.assertRaises(ForcedTermination):
                engine.run(fail_stage="quality")
            state = StageSnapshotStore(root / "state")
            committed = state.load_snapshots()
            self.assertEqual(
                [item["stage"] for item in committed],
                ["ingest", "template"],
            )
            (root / "state/stages/.tmp-uncommitted").write_text(
                "discard me", encoding="utf-8"
            )
            resumed_engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state",
                out_dir=root / "out",
            )

            def should_not_run(*args, **kwargs):
                raise AssertionError("committed stage was recomputed")

            with mock.patch(
                "ifdata.engine.run_ingest", should_not_run
            ), mock.patch("ifdata.engine.run_template", should_not_run):
                report = resumed_engine.run()
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["resumed_from_stage"], "template")
            self.assertEqual(report["started_at_stage"], "quality")
            self.assertEqual(
                [item["stage"] for item in report["newly_committed"]],
                ["quality", "dedup", "sampling", "export"],
            )
            self.assertTrue(report["state"]["discarded_temp_files"])
            events = [
                __import__("json").loads(line)
                for line in (root / "state/events.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(
                sum(
                    1
                    for event in events
                    if event["event"] == "stage_started"
                    and event["stage"] == "ingest"
                ),
                1,
            )

    def test_changed_input_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generate_fixture(root / "data", count=16)
            engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state",
                out_dir=root / "out",
            )
            engine.run(stop_after_stage="ingest")
            raw = root / "data/raw_samples.jsonl"
            raw.write_bytes(raw.read_bytes() + b"\n")
            fresh_engine = PipelineEngine(
                data_dir=root / "data",
                state_dir=root / "state",
                out_dir=root / "out",
            )
            with self.assertRaises(BusinessError) as context:
                fresh_engine.run()
            self.assertEqual(context.exception.code, E_SNAPSHOT_CORRUPT)


if __name__ == "__main__":
    unittest.main()

