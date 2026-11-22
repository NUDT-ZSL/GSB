from __future__ import annotations

from sft_pipeline.edits import (
    edit_quality_weight,
    edit_sample_output,
    remove_template,
    set_quota_target,
)
from sft_pipeline.engine import PipelineEngine

from .helpers import WorkspaceTestCase


class EditPropagationTests(WorkspaceTestCase):
    def test_quota_edit_reuses_entire_upstream(self) -> None:
        workspace = self.make_workspace()
        engine = PipelineEngine(workspace)
        engine.run()
        before = engine.store.load_manifest()
        edit = set_quota_target(workspace, "general_en", "high", 4)
        self.assertTrue(edit["changed"])
        run = engine.run()
        self.assertEqual(run.recomputed_stages, ["sample", "export"])
        self.assertEqual(
            run.reused_snapshot_stages,
            ["ingest", "normalize", "quality", "dedupe"],
        )
        after = engine.store.load_manifest()
        for stage in ("ingest", "normalize", "quality", "dedupe"):
            self.assertEqual(
                before["stages"][stage], after["stages"][stage]
            )
        self.assertEqual(after["stages"]["sample"]["version"], 7)
        self.assertEqual(after["stages"]["export"]["version"], 8)
        self.assertGreater(
            len(run.stage_diffs["sample"]["affected_sample_ids"]), 0
        )

    def test_template_edit_marks_only_matching_template_samples(self) -> None:
        workspace = self.make_workspace()
        engine = PipelineEngine(workspace)
        engine.run()
        normalize = engine.store.read_snapshot("normalize")["payload"]
        affected = [
            record["sample_id"]
            for record in normalize["records"]
            if record["template_id"] == "T-GEN-B"
        ]
        self.assertTrue(affected)
        remove_template(workspace, "T-GEN-B")
        run = engine.run()
        self.assertEqual(run.reused_snapshot_stages, ["ingest"])
        self.assertEqual(
            run.recomputed_stages,
            ["normalize", "quality", "dedupe", "sample", "export"],
        )
        diff = run.stage_diffs["normalize"]
        self.assertEqual(diff["affected_sample_ids"], sorted(affected))
        self.assertEqual(
            set(diff["reused_sample_ids"]),
            {
                record["sample_id"]
                for record in normalize["records"]
                if record["template_id"] != "T-GEN-B"
            },
        )

    def test_weight_edit_reuses_only_prefix_stages(self) -> None:
        workspace = self.make_workspace()
        engine = PipelineEngine(workspace)
        engine.run()
        edit_quality_weight(workspace, "noise", 0.2)
        run = engine.run()
        self.assertEqual(run.reused_snapshot_stages, ["ingest", "normalize"])
        self.assertEqual(
            run.recomputed_stages,
            ["quality", "dedupe", "sample", "export"],
        )
        self.assertEqual(
            run.stage_diffs["quality"]["affected_sample_ids"],
            pipeline_sample_ids(engine),
        )

    def test_output_edit_changes_ingest_for_exact_sample(self) -> None:
        workspace = self.make_workspace()
        engine = PipelineEngine(workspace)
        engine.run()
        export = engine.store.read_snapshot("export")["payload"]
        sample_id = export["rows"][0]["sample_id"]
        old_output = export["rows"][0]["output"]
        edit_sample_output(
            workspace, sample_id, old_output + " Additional local verification."
        )
        run = engine.run()
        self.assertEqual(
            run.stage_diffs["ingest"]["affected_sample_ids"], [sample_id]
        )
        self.assertEqual(
            run.stage_diffs["normalize"]["affected_sample_ids"], [sample_id]
        )
        self.assertEqual(run.reused_snapshot_stages, [])
        self.assertTrue(run.completed)


def pipeline_sample_ids(engine: PipelineEngine) -> list[str]:
    return sorted(
        record["sample_id"]
        for record in engine.store.read_snapshot("quality")["payload"]["records"]
    )
