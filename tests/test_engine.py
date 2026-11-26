from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fusion_diag.canonical import canonical_bytes
from fusion_diag.config import load_v1, write_v1
from fusion_diag.engine import (
    SimulatedCrash,
    load_workspace,
    query_one,
    run_batch,
)
from fusion_diag.errors import ResourceLimit, StageBusy
from fusion_diag.state import StageLock, StateStore

from .support import SampleWorkspace


def _line_for_query(path: str, query_id: str) -> bytes:
    for raw in Path(path).read_bytes().splitlines():
        value = json.loads(raw)
        if value["query_id"] == query_id:
            return canonical_bytes(value)
    raise AssertionError(f"query line not found: {query_id}")


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workspace = SampleWorkspace()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.workspace.cleanup()

    def _load(self, docs: str = "docs.jsonl", config: str = "config_v2"):
        return load_workspace(
            docs_path=self.workspace.path(docs),
            queries_path=self.workspace.path("queries.jsonl"),
            vocab_path=self.workspace.path("vocab.txt"),
            config_path=self.workspace.path(config),
        )

    def test_batch_and_interactive_bytes_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = self._load()
            output = Path(directory) / "out.jsonl"
            report = run_batch(
                loaded,
                workdir=Path(directory) / "work",
                output_path=output,
                mode="full",
                verify_full=True,
            )
            self.assertEqual(report["diff_coverage"]["status"], "match")
            result, delay = query_one(
                loaded,
                "q0006",
                workdir=Path(directory) / "work",
            )
            self.assertEqual(
                canonical_bytes(result),
                _line_for_query(str(output), "q0006"),
            )
            self.assertGreaterEqual(delay["observed_ms"], 0.0)
            self.assertEqual(
                delay["features"]["fusion_weight_count"], 4
            )

    def test_tf_revision_recomputes_only_dependent_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            base = self._load()
            run_batch(
                base,
                workdir=work,
                output_path=Path(directory) / "base.jsonl",
                mode="full",
            )
            revised = self._load("variants/docs_tf_revision.jsonl")
            report = run_batch(
                revised,
                workdir=work,
                output_path=Path(directory) / "revised.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertEqual(
                report["recomputed_query_ids"], ["q0001", "q0006"]
            )
            self.assertEqual(
                report["reused_query_ids"],
                ["q0002", "q0003", "q0004", "q0005", "q0007", "q0008", "q0009", "q0010", "q0011", "q0012"],
            )
            self.assertEqual(report["diff_coverage"]["status"], "match")

    def test_route_weight_change_is_targeted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            base = self._load()
            run_batch(
                base,
                workdir=work,
                output_path=Path(directory) / "base.jsonl",
                mode="full",
            )
            weighted = self._load(
                config="variants/config_prefix_weight_v1.json"
            )
            report = run_batch(
                weighted,
                workdir=work,
                output_path=Path(directory) / "weighted.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertIn("q0011", report["reused_query_ids"])
            self.assertNotIn("q0011", report["recomputed_query_ids"])
            self.assertIn("q0002", report["recomputed_query_ids"])
            self.assertLess(
                len(report["recomputed_query_ids"]),
                report["query_count"],
            )
            self.assertEqual(report["diff_coverage"]["status"], "match")

    def test_normalization_change_recomputes_all_queries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            base = self._load()
            run_batch(
                base,
                workdir=work,
                output_path=Path(directory) / "base.jsonl",
                mode="full",
            )
            ranked = self._load(config="variants/config_rank_norm_v1.json")
            report = run_batch(
                ranked,
                workdir=work,
                output_path=Path(directory) / "ranked.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertEqual(
                report["recomputed_query_ids"],
                [query.query_id for query in ranked.queries],
            )
            self.assertEqual(report["reused_query_ids"], [])
            self.assertEqual(report["diff_coverage"]["status"], "match")

    def test_switching_back_from_other_config_does_not_reuse_stale_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            base = self._load()
            base_output = Path(directory) / "base.jsonl"
            base_report = run_batch(
                base,
                workdir=work,
                output_path=base_output,
                mode="full",
            )
            ranked = self._load(config="variants/config_rank_norm_v1.json")
            run_batch(
                ranked,
                workdir=work,
                output_path=Path(directory) / "ranked.jsonl",
                mode="auto",
            )
            back = run_batch(
                base,
                workdir=work,
                output_path=Path(directory) / "back.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertEqual(back["output_sha256"], base_report["output_sha256"])
            self.assertEqual(back["diff_coverage"]["status"], "match")

    def test_v1_to_v2_snapshot_switch_rebuilds_config_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            v1 = self._load(config="config_v1.json")
            run_batch(
                v1,
                workdir=work,
                output_path=Path(directory) / "v1.jsonl",
                mode="full",
            )
            v2 = self._load(config="config_v2")
            report = run_batch(
                v2,
                workdir=work,
                output_path=Path(directory) / "v2.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertEqual(report["diff_coverage"]["status"], "match")
            for raw in (Path(directory) / "v2.jsonl").read_bytes().splitlines():
                value = json.loads(raw)
                self.assertEqual(value["config"]["schema"], "fusion_config.v2")

    def test_checkpoint_resume_and_no_duplicate_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "work"
            loaded = self._load()
            with self.assertRaises(SimulatedCrash):
                run_batch(
                    loaded,
                    workdir=work,
                    output_path=Path(directory) / "out.jsonl",
                    mode="auto",
                    crash_after="fusion",
                )
            state = StateStore(work / "state")
            self.assertEqual(len(state.get_all_committed()), 0)
            report = run_batch(
                loaded,
                workdir=work,
                output_path=Path(directory) / "out.jsonl",
                mode="auto",
                verify_full=True,
            )
            self.assertEqual(report["resumed_from"]["stage"], "fusion")
            self.assertEqual(report["committed_record_count"], 12)
            self.assertEqual(report["diff_coverage"]["status"], "match")
            self.assertEqual(state.commit_audit_count("q0006"), 1)
            rerun = run_batch(
                loaded,
                workdir=work,
                output_path=Path(directory) / "out2.jsonl",
                mode="auto",
            )
            self.assertEqual(rerun["recomputed_query_ids"], [])
            self.assertEqual(state.commit_audit_count("q0006"), 1)

    def test_stage_busy_is_returned_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stage.lock"
            outer = StageLock(path)
            outer.__enter__()
            try:
                with self.assertRaises(StageBusy) as caught:
                    StageLock(path).__enter__()
                self.assertEqual(caught.exception.code, "E_STAGE_BUSY")
            finally:
                outer.__exit__(None, None, None)

    def test_resource_limit_is_enforced_before_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = load_v1(self.workspace.path("config_v1.json"))
            payload = json.loads(json.dumps(source.payload))
            payload["fusion"]["memory_limit_mb"] = 1
            config_path = Path(directory) / "tiny.json"
            write_v1(
                config_path,
                payload,
                config_id="tiny-memory",
                effective_seconds=0.0,
            )
            loaded = load_workspace(
                docs_path=self.workspace.path("docs.jsonl"),
                queries_path=self.workspace.path("queries.jsonl"),
                vocab_path=self.workspace.path("vocab.txt"),
                config_path=config_path,
            )
            with self.assertRaises(ResourceLimit) as caught:
                run_batch(
                    loaded,
                    workdir=Path(directory) / "work",
                    output_path=Path(directory) / "out.jsonl",
                )
            self.assertEqual(caught.exception.code, "E_RESOURCE_LIMIT")


if __name__ == "__main__":
    unittest.main()
