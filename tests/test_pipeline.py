from __future__ import annotations

import hashlib
import json

from sft_pipeline.engine import PipelineEngine, pipeline_status
from sft_pipeline.stages import STAGE_NAMES
from sft_pipeline.util import canonical_json_text, sha256_bytes

from .helpers import WorkspaceTestCase


class PipelineTests(WorkspaceTestCase):
    def test_quota_underfill_is_explained(self) -> None:
        workspace = self.make_workspace("underfill")
        quotas = self.read_json(workspace.quotas_path)
        quotas["total_target"] = 100
        self.write_json(workspace.quotas_path, quotas)
        run = self.run_pipeline(workspace)
        sample = PipelineEngine(workspace).store.read_snapshot("sample")[
            "payload"
        ]
        self.assertGreater(sample["underfill"], 0)
        self.assertIn(
            "global_candidate_pool_shortfall", sample["total_reasons"]
        )
        self.assertTrue(
            any(
                "insufficient_candidates" in goal["reasons"]
                for goal in sample["goals"]
            )
        )
        self.assertEqual(sample["actual_total"], len(sample["records"]))
        self.assertTrue(run["completed"])

    def test_full_pipeline_is_byte_deterministic_and_self_consistent(self) -> None:
        first = self.make_workspace("first")
        second = self.make_workspace("second")
        first_run = self.run_pipeline(first)
        second_run = self.run_pipeline(second)
        self.assertTrue(first_run["completed"])
        self.assertTrue(second_run["completed"])
        self.assertEqual(
            self.export_bytes(first, first_run),
            self.export_bytes(second, second_run),
        )
        self.assertEqual(
            first_run["final_export"]["content_checksum"],
            second_run["final_export"]["content_checksum"],
        )
        first_manifest = PipelineEngine(first).store.load_manifest()
        second_manifest = PipelineEngine(second).store.load_manifest()
        for stage in STAGE_NAMES:
            first_snapshot = (
                first.root / first_manifest["stages"][stage]["snapshot"]
            ).read_bytes()
            second_snapshot = (
                second.root / second_manifest["stages"][stage]["snapshot"]
            ).read_bytes()
            self.assertEqual(first_snapshot, second_snapshot)

        manifest = first_manifest
        self.assertEqual(
            [manifest["stages"][stage]["version"] for stage in STAGE_NAMES],
            [1, 2, 3, 4, 5, 6],
        )
        check = PipelineEngine(first).self_check()
        self.assertTrue(check["ok"], check)
        self.assertTrue(check["complete"])

        rerun = self.run_pipeline(first)
        self.assertEqual(rerun["mode"], "complete_reuse")
        self.assertEqual(rerun["recomputed_stages"], [])
        self.assertEqual(rerun["reused_snapshot_stages"], list(STAGE_NAMES))
        status = pipeline_status(first)
        self.assertEqual(status["current_stage"], "complete")
        self.assertEqual(status["export"]["record_count"], 30)

    def test_dedupe_and_sampling_invariants(self) -> None:
        workspace = self.make_workspace()
        run = self.run_pipeline(workspace)
        store = PipelineEngine(workspace).store
        quality = store.read_snapshot("quality")["payload"]
        dedupe = store.read_snapshot("dedupe")["payload"]
        sample = store.read_snapshot("sample")["payload"]
        score = {
            record["sample_id"]: record["quality_score"]
            for record in quality["records"]
        }

        removed = [
            record
            for record in dedupe["records"]
            if not record["dedupe"]["kept"]
        ]
        self.assertGreater(len(removed), 0)
        for record in removed:
            detail = record["dedupe"]
            self.assertEqual(detail["threshold"], dedupe["threshold"])
            self.assertTrue(detail["similar_neighbors"])
            self.assertTrue(
                all(
                    neighbor["similarity"] >= dedupe["threshold"]
                    for neighbor in detail["similar_neighbors"]
                )
            )
        for cluster in dedupe["clusters"]:
            winner = min(
                cluster["member_ids"],
                key=lambda identifier: (-score[identifier], identifier),
            )
            self.assertEqual(cluster["representative_id"], winner)

        selected = [record["sample_id"] for record in sample["records"]]
        self.assertEqual(len(selected), len(set(selected)))
        self.assertEqual(sample["actual_total"], sample["total_target"])
        self.assertEqual(sample["underfill"], 0)
        self.assertTrue(
            set(selected).issubset(set(dedupe["kept_sample_ids"]))
        )
        for goal in sample["goals"]:
            actual = sum(
                1
                for record in sample["records"]
                if record["sampling"]["target_index"] == goal["target_index"]
            )
            self.assertEqual(actual, goal["actual"])
        self.assertTrue(run["final_export"]["dataset_bytes_checksum"])

    def test_export_header_checksum_and_order(self) -> None:
        workspace = self.make_workspace()
        run = self.run_pipeline(workspace)
        export_payload = PipelineEngine(workspace).store.read_snapshot(
            "export"
        )["payload"]
        raw = self.export_bytes(workspace, run)
        header_line, *record_lines = raw.decode("utf-8").splitlines()
        header = json.loads(header_line)
        rows = [json.loads(line) for line in record_lines]
        content = "".join(
            canonical_json_text(row) + "\n" for row in rows
        ).encode("utf-8")
        checksum = sha256_bytes(content)
        self.assertEqual(header["content_checksum"], f"sha256:{checksum}")
        self.assertEqual(header["record_count"], len(rows))
        self.assertEqual(
            [row["sample_id"] for row in rows],
            [
                record["sample_id"]
                for record in PipelineEngine(workspace)
                .store.read_snapshot("sample")["payload"]["records"]
            ],
        )
        self.assertEqual(
            hashlib.sha256(raw).hexdigest(),
            export_payload["dataset_bytes_checksum"],
        )
