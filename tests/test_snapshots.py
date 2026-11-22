from __future__ import annotations

import json

from sft_pipeline.engine import PipelineEngine
from sft_pipeline.errors import (
    BusinessError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_STATE_INCONSISTENT,
)

from .helpers import WorkspaceTestCase


class SnapshotProtectionTests(WorkspaceTestCase):
    def test_partial_stage_cannot_commit(self) -> None:
        workspace = self.make_workspace("partial")
        engine = PipelineEngine(workspace)
        with self.assertRaises(BusinessError) as raised:
            engine.store.commit_payload(
                stage="ingest",
                payload={
                    "stage": "ingest",
                    "records": [
                        {
                            "sample_id": "S0001",
                        }
                    ],
                    "completed_sample_ids": [],
                },
                cache_key="partial",
                upstream_checksum=None,
                dependency_digest="partial",
            )
        self.assertEqual(raised.exception.code, E_STATE_INCONSISTENT)

    def test_rollback_busy_and_corruption(self) -> None:
        workspace = self.make_workspace()
        engine = PipelineEngine(workspace)
        engine.run()
        state = engine.store.load_manifest()
        sample_entry = state["stages"]["sample"]

        with self.assertRaises(BusinessError) as rollback:
            engine.store.commit_payload(
                stage="sample",
                payload={
                    "stage": "sample",
                    "completed_sample_ids": [],
                    "records": [],
                    "actual_total": 0,
                    "candidate_count": 0,
                },
                cache_key=sample_entry["cache_key"],
                upstream_checksum=state["stages"]["dedupe"]["checksum"],
                dependency_digest=sample_entry["dependency_digest"],
                requested_version=sample_entry["version"],
            )
        self.assertEqual(rollback.exception.code, E_SNAPSHOT_ROLLBACK)

        with engine.store.commit_lock:
            with self.assertRaises(BusinessError) as busy:
                engine.store.commit_payload(
                    stage="ingest",
                    payload={"stage": "ingest", "completed_sample_ids": []},
                    cache_key="x",
                    upstream_checksum=None,
                    dependency_digest="x",
                )
        self.assertEqual(busy.exception.code, E_STAGE_BUSY)

        quality_entry = state["stages"]["quality"]
        snapshot_path = workspace.root / quality_entry["snapshot"]
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["payload"]["records"][0]["output"] += " tampered"
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(BusinessError) as corrupt:
            engine.self_check()
        self.assertEqual(corrupt.exception.code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(corrupt.exception.stage, "quality")
        self.assertEqual(
            corrupt.exception.details["version"], quality_entry["version"]
        )
