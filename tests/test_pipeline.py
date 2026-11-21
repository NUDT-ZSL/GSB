from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deploylab import LabConfig, Pipeline
from deploylab.bundle import load_bundle, write_bundle, write_revision_bundle
from deploylab.config import BudgetConfig
from deploylab.errors import (
    DeployError,
    E_INCONSISTENT,
    E_PARTIAL_STAGE,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    STAGES,
)
from deploylab.store import SnapshotStore


SMALL_SPECS = [
    {
        "layer_id": "L00",
        "layer_shape": [8, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_tensor:group=4",
        "calib_seed": 2101,
    },
    {
        "layer_id": "L01",
        "layer_shape": [8, 8],
        "quant_dtype": "int8",
        "scale_hint": "per_channel:group=4",
        "calib_seed": 2102,
    },
    {
        "layer_id": "L02",
        "layer_shape": [6, 8],
        "quant_dtype": "int4",
        "scale_hint": "per_channel:group=4",
        "calib_seed": 2103,
    },
    {
        "layer_id": "L03",
        "layer_shape": [6, 6],
        "quant_dtype": "int4",
        "scale_hint": "per_tensor",
        "calib_seed": 2104,
    },
    {
        "layer_id": "L04",
        "layer_shape": [4, 6],
        "quant_dtype": "int8",
        "scale_hint": "per_channel",
        "calib_seed": 2105,
    },
]


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.bundle_path = cls.root / "bundle"
        cls.bundle = write_bundle(
            cls.bundle_path,
            specs=SMALL_SPECS,
            calib_samples=4,
        )
        cls.layer_ids = [layer.layer_id for layer in cls.bundle.layers]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def db(self, name: str) -> Path:
        return self.root / f"{name}.sqlite"

    def test_full_run_atomic_stages_and_deterministic_replay(self) -> None:
        first = Pipeline(self.bundle_path, self.db("deterministic_a")).run()
        second = Pipeline(self.bundle_path, self.db("deterministic_b")).run()
        self.assertEqual(first["prediction"]["b64"], second["prediction"]["b64"])
        self.assertEqual(
            first["activation_checksums"], second["activation_checksums"]
        )
        self.assertEqual([row["version"] for row in first["snapshots"]], [1, 2, 3, 4])
        self.assertEqual([row["stage"] for row in first["snapshots"]], list(STAGES))
        for snapshot in first["snapshots"]:
            self.assertEqual(snapshot["completed_layers"], self.layer_ids)
        self.assertEqual(first["checkpoint_chain_problems"], [])
        store = SnapshotStore(self.db("deterministic_a"))
        self.assertEqual(store.verify_chain(), [])

    def test_partial_stage_commit_is_rejected_and_invisible(self) -> None:
        db_path = self.db("partial")
        Pipeline(self.bundle_path, db_path).run()
        store = SnapshotStore(db_path)
        latest = store.latest_generation()
        before = store.latest_version()
        manifest = latest["manifest"]
        with self.assertRaises(DeployError) as context:
            store.commit_stage(
                generation=latest["generation"] + 1,
                stage="model_validation",
                completed_layers=self.layer_ids[:-1],
                artifacts=[
                    {
                        "layer_id": layer_id,
                        "cache_key": f"partial-{layer_id}",
                        "payload": "{}",
                        "disposition": "recomputed",
                    }
                    for layer_id in self.layer_ids[:-1]
                ],
                metadata={},
                manifest=manifest,
                calibration_config=latest["calibration_config"],
                invalidation=latest["invalidation"],
            )
        self.assertEqual(context.exception.code, E_PARTIAL_STAGE)
        self.assertEqual(store.latest_version(), before)
        self.assertEqual(len(store.generation_snapshots(latest["generation"])), 4)

    def test_concurrent_stage_commit_returns_busy_immediately(self) -> None:
        db_path = self.db("busy")
        Pipeline(self.bundle_path, db_path).run()
        store = SnapshotStore(db_path)
        latest = store.latest_generation()
        manifest = latest["manifest"]
        artifacts = [
            {
                "layer_id": layer_id,
                "cache_key": f"busy-{layer_id}",
                "payload": "{}",
                "disposition": "recomputed",
            }
            for layer_id in self.layer_ids
        ]
        with store.writer_guard():
            with self.assertRaises(DeployError) as context:
                store.commit_stage(
                    generation=latest["generation"],
                    stage="model_validation",
                    completed_layers=self.layer_ids,
                    artifacts=artifacts,
                    metadata={},
                    manifest=manifest,
                    calibration_config=latest["calibration_config"],
                    invalidation=latest["invalidation"],
                )
        self.assertEqual(context.exception.code, E_STAGE_BUSY)

    def test_revision_invalidation_reuses_upstream_and_matches_clean_run(self) -> None:
        db_path = self.db("revision")
        clean_db = self.db("revision_clean")
        revision_bundle = self.root / "bundle_rev"
        write_revision_bundle(
            self.bundle_path,
            revision_bundle,
            layer_id="L02",
            weight_revision=2,
        )
        Pipeline(self.bundle_path, db_path).run()
        pipeline = Pipeline(revision_bundle, db_path)
        updated = pipeline.run()
        clean = Pipeline(revision_bundle, clean_db).run()
        invalidation = updated["invalidation"]
        self.assertEqual(invalidation["changed_layers"], ["L02"])
        self.assertEqual(
            invalidation["affected_layers"], ["L02", "L03", "L04"]
        )
        self.assertEqual(invalidation["reused_layers"], ["L00", "L01"])
        self.assertFalse(
            set(invalidation["affected_layers"])
            & set(invalidation["reused_layers"])
        )
        self.assertEqual(
            set(invalidation["affected_layers"])
            | set(invalidation["reused_layers"]),
            set(self.layer_ids),
        )
        self.assertEqual(updated["prediction"]["b64"], clean["prediction"]["b64"])
        for stage in ("quantization", "calibration", "inference"):
            self.assertEqual(
                updated["recovery"]["reused_layers_by_stage"][stage],
                ["L00", "L01"],
            )
            self.assertEqual(
                updated["recovery"]["rebuilt_layers_by_stage"][stage],
                ["L02", "L03", "L04"],
            )
        quant_artifacts = pipeline.store.load_stage_artifacts(
            updated["generation"], "quantization"
        )
        self.assertEqual(
            quant_artifacts["L02"]["payload"]["weight_revision"], 2
        )
        self.assertEqual(
            quant_artifacts["L01"]["payload"]["weight_revision"], 1
        )
        self.assertTrue(pipeline.verify_consistency()["ok"])

    def test_calibration_update_invalidates_layer_and_downstream(self) -> None:
        db_path = self.db("calibration_update")
        Pipeline(self.bundle_path, db_path).run()
        pipeline = Pipeline(
            self.bundle_path,
            db_path,
            calibration_updates={"L01": {"revision": 1, "seed": 9999}},
        )
        result = pipeline.run()
        self.assertEqual(result["invalidation"]["changed_layers"], ["L01"])
        self.assertEqual(
            result["invalidation"]["affected_layers"],
            ["L01", "L02", "L03", "L04"],
        )
        self.assertEqual(result["invalidation"]["reused_layers"], ["L00"])
        self.assertTrue(pipeline.verify_consistency()["ok"])

    def test_revision_rollback_is_rejected(self) -> None:
        db_path = self.db("rollback")
        revision_bundle = self.root / "bundle_rollback_base"
        write_revision_bundle(
            self.bundle_path,
            revision_bundle,
            layer_id="L02",
            weight_revision=2,
        )
        Pipeline(revision_bundle, db_path).run()
        with self.assertRaises(DeployError) as context:
            Pipeline(self.bundle_path, db_path).run()
        self.assertEqual(context.exception.code, E_SNAPSHOT_ROLLBACK)
        store = SnapshotStore(db_path)
        self.assertEqual(store.latest_generation()["generation"], 1)

    def test_resource_limit_and_degradation_categories(self) -> None:
        hard = LabConfig(
            calib_samples=4,
            budget=BudgetConfig(max_layers_per_stage=2),
        )
        with self.assertRaises(DeployError) as context:
            Pipeline(
                self.bundle_path,
                self.db("resource_hard"),
                config=hard,
            ).run()
        self.assertEqual(context.exception.code, E_RESOURCE_LIMIT)

        degraded_config = LabConfig(
            calib_samples=4,
            budget=BudgetConfig(
                max_resident_bytes=4_000,
                degraded_group_size=4,
            ),
        )
        pipeline = Pipeline(
            self.bundle_path,
            self.db("resource_degraded"),
            config=degraded_config,
        )
        result = pipeline.run()
        self.assertIn(
            "DEGRADED_GROUP_SIZE",
            result["budget_ledgers"]["quantization"]["degradations"],
        )
        self.assertTrue(pipeline.verify_consistency()["ok"])

    def test_old_revision_is_never_mixed_into_inference(self) -> None:
        db_path = self.db("old_revision")
        revision_bundle = self.root / "bundle_revision_check"
        write_revision_bundle(
            self.bundle_path,
            revision_bundle,
            layer_id="L01",
            weight_revision=3,
        )
        Pipeline(self.bundle_path, db_path).run()
        pipeline = Pipeline(revision_bundle, db_path)
        result = pipeline.run()
        generation = result["generation"]
        quant = pipeline.store.load_stage_artifacts(generation, "quantization")
        inference = pipeline.store.load_stage_artifacts(generation, "inference")
        for layer in load_bundle(revision_bundle).layers:
            self.assertEqual(
                quant[layer.layer_id]["payload"]["weight_revision"],
                layer.weight_revision,
            )
            self.assertEqual(
                inference[layer.layer_id]["payload"]["quant_artifact_digest"],
                quant[layer.layer_id]["digest"],
            )

    def test_consistency_checker_returns_category(self) -> None:
        db_path = self.db("tamper")
        pipeline = Pipeline(self.bundle_path, db_path)
        result = pipeline.run()
        with sqlite3.connect(db_path) as conn:
            bad = {
                "trigger": "tampered",
                "changed_layers": ["L02"],
                "affected_layers": ["L02"],
                "reused_layers": ["L00"],
                "recomputed_layers": ["L02"],
                "full_layers": self.layer_ids,
            }
            conn.execute(
                "UPDATE generations SET invalidation_json=? WHERE generation=?",
                (json.dumps(bad), result["generation"]),
            )
            conn.commit()
        with self.assertRaises(DeployError) as context:
            pipeline.verify_consistency()
        self.assertEqual(context.exception.code, E_INCONSISTENT)
        self.assertIn(
            "invalidation_sets",
            context.exception.details["categories"],
        )


if __name__ == "__main__":
    unittest.main()

