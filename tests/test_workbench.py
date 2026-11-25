from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from modelwb import Workbench, WorkbenchError
from modelwb.api import create_app
from modelwb.canonical import sha256_json
from modelwb.seed import generate


ROOT = Path(__file__).resolve().parents[1]


class WorkbenchTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sample_temp = tempfile.TemporaryDirectory(prefix="modelwb-samples-")
        cls.samples = Path(cls.sample_temp.name) / "samples"
        generate(cls.samples)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sample_temp.cleanup()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="modelwb-test-")
        self.db_path = Path(self.temp.name) / "state.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def new_workbench(self, name: str = "state.db") -> Workbench:
        return Workbench(Path(self.temp.name) / name)

    def loaded(self, name: str = "state.db") -> Workbench:
        workbench = self.new_workbench(name)
        workbench.load(input_dir=self.samples)
        return workbench

    def patched(self, name: str = "state.db") -> Workbench:
        workbench = self.loaded(name)
        workbench.patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        return workbench

    def mutate_patch(self, name: str, mutator) -> Path:
        data = json.loads((self.samples / "patch_v1_to_v2.json").read_text("utf-8"))
        mutator(data)
        data.pop("patch_sha256", None)
        data["patch_sha256"] = sha256_json(data)
        path = Path(self.temp.name) / name
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        return path

    def assert_error(self, code: str, function, *args, **kwargs):
        with self.assertRaises(WorkbenchError) as context:
            function(*args, **kwargs)
        self.assertEqual(context.exception.code, code, context.exception.to_dict())
        return context.exception


class MergeAndValidationTests(WorkbenchTestBase):
    def test_merge_precedence_time_and_tombstone(self) -> None:
        workbench = self.loaded()
        view = workbench.versions()
        entries = {entry["version"]: entry for entry in view["versions"]}
        self.assertEqual(entries["shared-model-v1"]["winner_source"], "v2")
        self.assertEqual(entries["aux-v1"]["winner_source"], "v2")
        self.assertFalse(entries["retired-v1"]["active"])
        self.assertTrue(entries["retired-v1"]["tombstone"])
        classifier = entries["classifier-v1"]
        self.assertEqual(classifier["absolute_updated_ms"], 1_700_000_001_501)
        provenance = classifier["time_interop"]["provenance"]
        self.assertEqual(provenance["scale_ms"], "1000")
        self.assertEqual(provenance["rounding"], "half_up")
        self.assertEqual(provenance["rounding_direction"], "up")
        self.assertEqual(
            classifier["time_interop"]["normalized_json"],
            '{"absolute_updated_ms":1700000001501}',
        )
        repeated = workbench.versions()["versions"]
        self.assertEqual(view["versions"], repeated)
        shared = entries["shared-model-v1"]
        self.assertEqual(len(shared["candidates"]), 2)
        self.assertEqual(
            [candidate["source_format"] for candidate in shared["candidates"]],
            ["v1", "v2"],
        )
        catalog = workbench.storage.catalog()
        self.assertEqual(
            catalog.manifest("shared-model-v1", "v1").time_interop()["normalized_json"],
            catalog.manifest("shared-model-v1", "v2").time_interop()["normalized_json"],
        )

    def test_config_cycle_error(self) -> None:
        registries = json.loads((self.samples / "registries.json").read_text("utf-8"))
        registries["configs"]["shared"]["cycle-a"] = {
            "kind": "shared",
            "references": ["cycle-b"],
            "feature_tables": [],
            "upstream_models": [],
        }
        registries["configs"]["shared"]["cycle-b"] = {
            "kind": "shared",
            "references": ["cycle-a"],
            "feature_tables": [],
            "upstream_models": [],
        }
        cycle_file = Path(self.temp.name) / "cycle-registries.json"
        cycle_file.write_text(json.dumps(registries), encoding="utf-8")
        workbench = self.new_workbench()
        error = self.assert_error(
            "CONFIG_REFERENCE_CYCLE",
            workbench.load,
            input_dir=self.samples,
            registries_path=cycle_file,
        )
        self.assertEqual(error.business_code, "MWB-1004")

    def test_version_duplicate_and_order_validation(self) -> None:
        v1 = json.loads((self.samples / "v1_manifest.json").read_text("utf-8"))
        v1["versions"][1]["version"] = "shared-model-v1"
        duplicate = Path(self.temp.name) / "duplicate-v1.json"
        duplicate.write_text(json.dumps(v1), encoding="utf-8")
        workbench = self.new_workbench()
        self.assert_error(
            "VERSION_DUPLICATE",
            workbench.load,
            v1_path=duplicate,
            v2_db_path=self.samples / "v2" / "meta.db",
            v2_dir=self.samples / "v2",
            replicas_path=self.samples / "replicas.json",
            baseline_version="classifier-v1",
        )


class PatchAndInvalidationTests(WorkbenchTestBase):
    def test_patch_matches_full_replacement_and_invalidates_closure(self) -> None:
        workbench = self.loaded()
        before_caches = {
            cache["cache_id"]: cache for cache in workbench.storage.list_caches()
        }
        verification = workbench.verify_patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        self.assertTrue(verification["matches_full_replacement"])
        result = workbench.patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        self.assertEqual(
            result["directory_digest"], result["full_replacement_directory_digest"]
        )
        self.assertEqual(result["unchanged_shards"], 4)
        self.assertEqual(result["update_round"], 1)
        invalidated = set(result["invalidation"]["invalidated_cache_ids"])
        reusable = set(result["invalidation"]["reusable_cache_ids"])
        self.assertEqual(
            invalidated,
            {
                "cache-a-warmup",
                "cache-a-inference",
                "cache-b-warmup",
                "cache-b-inference",
                "cache-d-warmup",
                "cache-d-inference",
            },
        )
        self.assertEqual(reusable, {"cache-c-warmup", "cache-c-inference"})
        after_caches = {
            cache["cache_id"]: cache for cache in workbench.storage.list_caches()
        }
        for cache_id in reusable:
            self.assertTrue(after_caches[cache_id]["valid"])
            self.assertEqual(
                after_caches[cache_id]["shard_chain_digest"],
                before_caches[cache_id]["shard_chain_digest"],
            )
            self.assertEqual(
                after_caches[cache_id]["shard_chain"],
                before_caches[cache_id]["shard_chain"],
            )
        for cache_id in invalidated:
            self.assertFalse(after_caches[cache_id]["valid"])
        status = workbench.status()
        self.assertEqual(status["current_snapshot"]["version"], "classifier-v2")
        self.assertEqual(status["update_round_watermark"], 1)
        self.assertEqual(status["release"]["current_ratio"], 0)

    def test_shard_missing_and_layer_missing_codes(self) -> None:
        patch = self.mutate_patch(
            "missing-shard.json",
            lambda data: data["changes"][0].update({"shard_id": "embedding-999"}),
        )
        workbench = self.loaded("shard.db")
        error = self.assert_error(
            "SHARD_MISSING",
            workbench.patch,
            patch_path=patch,
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        self.assertEqual(error.business_code, "MWB-1001")

        layer_patch = self.mutate_patch(
            "missing-layer.json",
            lambda data: data["changes"][0].update({"layer": "missing-layer"}),
        )
        workbench2 = self.loaded("layer.db")
        error = self.assert_error(
            "LAYER_REFERENCE_MISSING",
            workbench2.patch,
            patch_path=layer_patch,
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        self.assertEqual(error.business_code, "MWB-1003")

    def test_digest_mismatch_and_undeclared_change(self) -> None:
        def corrupt_digest(data):
            data["changes"][0]["sha256"] = "0" * 64

        patch = self.mutate_patch("bad-digest.json", corrupt_digest)
        workbench = self.loaded("digest.db")
        error = self.assert_error(
            "SHARD_DIGEST_MISMATCH",
            workbench.patch,
            patch_path=patch,
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        self.assertEqual(error.business_code, "MWB-1002")
        self.assertEqual(error.diagnostics["first_divergence"]["shard_id"], "embedding-001")

        full = json.loads(
            (self.samples / "full_replacement" / "classifier-v2.json").read_text("utf-8")
        )
        encoder = next(layer for layer in full["layers"] if layer["name"] == "encoder")
        encoder["shards"][0]["sha256"] = "1" * 64
        altered_full = Path(self.temp.name) / "altered-full.json"
        altered_full.write_text(json.dumps(full), encoding="utf-8")
        workbench2 = self.loaded("undeclared.db")
        error = self.assert_error(
            "PATCH_UNDECLARED_SHARD_CHANGE",
            workbench2.verify_patch,
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=altered_full,
        )
        self.assertEqual(error.business_code, "MWB-1202")
        self.assertEqual(error.diagnostics["first_divergence"]["layer"], "encoder")
        self.assertEqual(error.diagnostics["first_divergence"]["shard_id"], "encoder-000")

    def test_clock_rollback_code(self) -> None:
        workbench = self.loaded()
        error = self.assert_error(
            "CLOCK_ROLLBACK",
            workbench.patch,
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
            clock_tolerance=0,
        )
        self.assertEqual(error.business_code, "MWB-1006")
        self.assertEqual(error.diagnostics["watermark"] - error.diagnostics["logical_clock"], 10)


class GrayRollbackTests(WorkbenchTestBase):
    def test_gray_bindings_and_rollback(self) -> None:
        workbench = self.loaded()
        baseline_request = workbench.start_request("req-baseline")
        self.assertEqual(baseline_request["model_version"], "classifier-v1")
        workbench.patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        release = workbench.storage.latest_release()
        gray = workbench.gray(
            target_version="classifier-v2",
            stages=[25, 75, 100],
            run_id=release["run_id"],
        )
        self.assertEqual(gray["status"], "gray_complete")
        self.assertEqual([stage["ratio"] for stage in gray["stages"]], [25, 75, 100])
        target_request = workbench.start_request("req-target")
        self.assertEqual(target_request["model_version"], "classifier-v2")
        self.assertEqual(
            target_request["binding"]["directory_digest"], gray["health"]["directory_digest"]
        )
        binding_before_rollback = target_request["binding"]
        rollback = workbench.rollback(
            run_id=release["run_id"],
            reason="observed_regression",
            trigger="monitor",
            window_ms=2500,
        )
        self.assertEqual(rollback["trigger"]["source"], "monitor")
        self.assertEqual(rollback["trigger"]["current_ratio"], 100)
        self.assertEqual(rollback["trigger"]["gray_replicas"], ["replica-a", "replica-b"])
        self.assertEqual(rollback["restored_version"], "classifier-v1")
        status = workbench.status()
        self.assertEqual(status["current_snapshot"]["version"], "classifier-v1")
        self.assertEqual(status["release"]["status"], "rolled_back")
        self.assertEqual(
            [step["to_ratio"] for step in status["release"]["rollback_steps"]],
            [75, 25, 0],
        )
        self.assertEqual(
            workbench.storage.get_request("req-target")["binding"],
            binding_before_rollback,
        )
        for replica in status["replicas"]:
            if replica["replica_id"] in ("replica-a", "replica-b"):
                self.assertEqual(replica["version_binding"], "classifier-v1")
                self.assertEqual(replica["stage"], "warmed")
        new_request = workbench.start_request("req-after-rollback")
        self.assertEqual(new_request["model_version"], "classifier-v1")

    def test_invalid_gray_ratio(self) -> None:
        workbench = self.patched()
        error = self.assert_error(
            "GRAY_RATIO_INVALID",
            workbench.gray,
            target_version="classifier-v2",
            stages=[25, 25, 100],
        )
        self.assertEqual(error.business_code, "MWB-1005")

    def test_prewarm_failure_blocks_gray_then_rolls_back(self) -> None:
        workbench = self.patched()
        release = workbench.storage.latest_release()
        error = self.assert_error(
            "SHARD_DIGEST_MISMATCH",
            workbench.gray,
            target_version="classifier-v2",
            stages=[25, 75, 100],
            run_id=release["run_id"],
            fail_replica="replica-a",
        )
        self.assertTrue(error.diagnostics["simulated"])
        status = workbench.status()
        self.assertEqual(status["release"]["status"], "failed")
        self.assertEqual(status["release"]["gray_stages"], [])
        replica_a = next(item for item in status["replicas"] if item["replica_id"] == "replica-a")
        self.assertNotEqual(replica_a["stage"], "gray")
        rollback = workbench.rollback(
            run_id=release["run_id"], reason="prewarm_failure", trigger="warmup"
        )
        self.assertEqual(rollback["restored_version"], "classifier-v1")


class RecoveryTests(WorkbenchTestBase):
    def run_cli(
        self,
        db_path: Path,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "modelwb", "--db", str(db_path)] + args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_crash_before_patch_commit_and_resume(self) -> None:
        clean_db = Path(self.temp.name) / "clean.db"
        clean = Workbench(clean_db)
        clean.load(input_dir=self.samples)
        clean_result = clean.patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        crash_db = Path(self.temp.name) / "crash.db"
        self.assertEqual(
            self.run_cli(crash_db, ["load", "--input-dir", str(self.samples)]).returncode,
            0,
        )
        env = dict(os.environ, MODELWB_CRASH_AT="patch_before_commit")
        crash = self.run_cli(
            crash_db,
            [
                "patch",
                "--patch",
                str(self.samples / "patch_v1_to_v2.json"),
                "--full",
                str(self.samples / "full_replacement" / "classifier-v2.json"),
            ],
            env=env,
        )
        self.assertEqual(crash.returncode, 86)
        interrupted = Workbench(crash_db).status()
        self.assertEqual(interrupted["current_snapshot"]["version"], "classifier-v1")
        self.assertEqual(
            [(item["kind"], item["status"]) for item in interrupted["operations"]],
            [("load", "committed"), ("patch", "running")],
        )
        resumed = self.run_cli(crash_db, ["resume"])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        recovered = Workbench(crash_db).status()
        self.assertEqual(recovered["current_snapshot"]["version"], "classifier-v2")
        self.assertEqual(
            recovered["current_snapshot"]["directory_digest"],
            clean_result["directory_digest"],
        )
        patch_snapshots = [
            snapshot for snapshot in Workbench(crash_db).storage.list_snapshots() if snapshot["kind"] == "patch"
        ]
        self.assertEqual(len(patch_snapshots), 1)

    def test_crash_during_gray_stage_and_resume(self) -> None:
        db_path = Path(self.temp.name) / "gray-crash.db"
        self.assertEqual(self.run_cli(db_path, ["load", "--input-dir", str(self.samples)]).returncode, 0)
        self.assertEqual(
            self.run_cli(
                db_path,
                [
                    "patch",
                    "--patch",
                    str(self.samples / "patch_v1_to_v2.json"),
                    "--full",
                    str(self.samples / "full_replacement" / "classifier-v2.json"),
                ],
            ).returncode,
            0,
        )
        env = dict(os.environ, MODELWB_CRASH_AT="gray_after_stage:2")
        crash = self.run_cli(
            db_path,
            ["gray", "--target-version", "classifier-v2", "--stages", "25,75,100"],
            env=env,
        )
        self.assertEqual(crash.returncode, 86)
        interrupted = Workbench(db_path).status()
        self.assertEqual(interrupted["release"]["current_ratio"], 75)
        self.assertEqual(len(interrupted["release"]["gray_stages"]), 2)
        self.assertEqual(
            next(op for op in interrupted["operations"] if op["kind"] == "gray")["status"],
            "running",
        )
        resumed = self.run_cli(db_path, ["resume"])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        recovered = Workbench(db_path).status()
        self.assertEqual(recovered["release"]["status"], "gray_complete")
        self.assertEqual(recovered["release"]["current_ratio"], 100)
        self.assertEqual(len(recovered["release"]["gray_stages"]), 3)
        self.assertEqual(
            {replica["stage"] for replica in recovered["replicas"] if replica["replica_id"] in ("replica-a", "replica-b")},
            {"gray"},
        )
        self.assertEqual(
            next(op for op in recovered["operations"] if op["kind"] == "gray")["status"],
            "committed",
        )

    def test_crash_after_patch_commit_does_not_reapply(self) -> None:
        db_path = Path(self.temp.name) / "after-commit.db"
        self.assertEqual(self.run_cli(db_path, ["load", "--input-dir", str(self.samples)]).returncode, 0)
        env = dict(os.environ, MODELWB_CRASH_AT="patch_after_commit")
        crash = self.run_cli(
            db_path,
            [
                "patch",
                "--patch",
                str(self.samples / "patch_v1_to_v2.json"),
                "--full",
                str(self.samples / "full_replacement" / "classifier-v2.json"),
            ],
            env=env,
        )
        self.assertEqual(crash.returncode, 86)
        workbench = Workbench(db_path)
        status = workbench.status()
        self.assertEqual(status["current_snapshot"]["version"], "classifier-v2")
        patch_operations = [op for op in status["operations"] if op["kind"] == "patch"]
        self.assertEqual(len(patch_operations), 1)
        self.assertEqual(patch_operations[0]["status"], "committed")
        patch_snapshot_count = len(
            [snapshot for snapshot in workbench.storage.list_snapshots() if snapshot["kind"] == "patch"]
        )
        resumed = self.run_cli(db_path, ["resume"])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(
            len(
                [
                    snapshot
                    for snapshot in Workbench(db_path).storage.list_snapshots()
                    if snapshot["kind"] == "patch"
                ]
            ),
            patch_snapshot_count,
        )

    def test_crash_during_rollback_and_resume(self) -> None:
        db_path = Path(self.temp.name) / "rollback-crash.db"
        workbench = Workbench(db_path)
        workbench.load(input_dir=self.samples)
        workbench.patch(
            patch_path=self.samples / "patch_v1_to_v2.json",
            full_path=self.samples / "full_replacement" / "classifier-v2.json",
        )
        workbench.gray(target_version="classifier-v2", stages=[25, 75, 100])
        release = workbench.storage.latest_release()
        env = dict(os.environ, MODELWB_CRASH_AT="rollback_after_step:1")
        crash = self.run_cli(
            db_path,
            [
                "rollback",
                "--run-id",
                release["run_id"],
                "--reason",
                "failure",
                "--trigger",
                "monitor",
            ],
            env=env,
        )
        self.assertEqual(crash.returncode, 86)
        interrupted = Workbench(db_path).status()
        self.assertEqual(interrupted["release"]["status"], "rolling_back")
        self.assertEqual(len(interrupted["release"]["rollback_steps"]), 1)
        self.assertEqual(
            interrupted["release"]["trigger"]["rollback_steps"],
            [
                {"from_ratio": 100, "to_ratio": 75},
                {"from_ratio": 75, "to_ratio": 25},
                {"from_ratio": 25, "to_ratio": 0},
            ],
        )
        resumed = self.run_cli(db_path, ["resume"])
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        recovered = Workbench(db_path).status()
        self.assertEqual(recovered["release"]["status"], "rolled_back")
        self.assertEqual(recovered["current_snapshot"]["version"], "classifier-v1")
        self.assertEqual(len(recovered["release"]["rollback_steps"]), 3)


class ApiAndSeedTests(WorkbenchTestBase):
    def test_flask_api(self) -> None:
        app = create_app(Path(self.temp.name) / "api.db")
        client = app.test_client()
        response = client.post(
            "/api/load", json={"input_dir": str(self.samples), "baseline_version": "classifier-v1"}
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = client.post(
            "/api/patch",
            json={
                "patch_path": str(self.samples / "patch_v1_to_v2.json"),
                "full_path": str(self.samples / "full_replacement" / "classifier-v2.json"),
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = client.post(
            "/api/gray",
            json={"target_version": "classifier-v2", "stages": [50, 100]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        response = client.post("/api/requests", json={"request_id": "http-request"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["model_version"], "classifier-v2")
        response = client.post(
            "/api/gray",
            json={"target_version": "classifier-v2", "stages": [10, 10]},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["error"]["business_code"], "MWB-1005")
        response = client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["current_snapshot"]["version"], "classifier-v2")
        response = client.get("/api/versions")
        self.assertEqual(response.status_code, 200)
        self.assertIn("versions", response.get_json())

    def test_seed_is_deterministic(self) -> None:
        first = Path(self.temp.name) / "first"
        second = Path(self.temp.name) / "second"
        generate(first)
        generate(second)
        self.assertEqual(
            (first / "MANIFEST.json").read_bytes(),
            (second / "MANIFEST.json").read_bytes(),
        )
        first_db = sqlite3.connect(first / "v2" / "meta.db")
        second_db = sqlite3.connect(second / "v2" / "meta.db")
        try:
            rows_first = first_db.execute(
                "SELECT version, canonical_id, revision, absolute_updated_ms, logical_clock, "
                "generation, patch_digest, tombstone FROM versions ORDER BY version"
            ).fetchall()
            rows_second = second_db.execute(
                "SELECT version, canonical_id, revision, absolute_updated_ms, logical_clock, "
                "generation, patch_digest, tombstone FROM versions ORDER BY version"
            ).fetchall()
        finally:
            first_db.close()
            second_db.close()
        self.assertEqual(rows_first, rows_second)


class CliFlowTests(WorkbenchTestBase):
    def run_cli(self, db_path: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "modelwb", "--db", str(db_path)] + args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_all_required_cli_subcommands(self) -> None:
        db_path = Path(self.temp.name) / "cli.db"
        load = self.run_cli(db_path, ["load", "--input-dir", str(self.samples)])
        self.assertEqual(load.returncode, 0, load.stderr)
        verify = self.run_cli(
            db_path,
            [
                "verify",
                "--patch",
                str(self.samples / "patch_v1_to_v2.json"),
                "--full",
                str(self.samples / "full_replacement" / "classifier-v2.json"),
            ],
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertTrue(json.loads(verify.stdout)["matches_full_replacement"])
        patch = self.run_cli(
            db_path,
            [
                "patch",
                "--patch",
                str(self.samples / "patch_v1_to_v2.json"),
                "--full",
                str(self.samples / "full_replacement" / "classifier-v2.json"),
            ],
        )
        self.assertEqual(patch.returncode, 0, patch.stderr)
        gray = self.run_cli(
            db_path,
            ["gray", "--target-version", "classifier-v2", "--stages", "25,75,100"],
        )
        self.assertEqual(gray.returncode, 0, gray.stderr)
        request_result = self.run_cli(
            db_path, ["request", "--request-id", "cli-request"]
        )
        self.assertEqual(request_result.returncode, 0, request_result.stderr)
        self.assertEqual(json.loads(request_result.stdout)["model_version"], "classifier-v2")
        rollback = self.run_cli(
            db_path,
            ["rollback", "--reason", "manual", "--trigger", "operator"],
        )
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        status = self.run_cli(db_path, ["status", "--compact"])
        self.assertEqual(status.returncode, 0, status.stderr)
        state = json.loads(status.stdout)
        self.assertEqual(state["current_snapshot"]["version"], "classifier-v1")
        self.assertEqual(state["release"]["status"], "rolled_back")
        self.assertEqual(
            next(item for item in state["requests"] if item["request_id"] == "cli-request")[
                "model_version"
            ],
            "classifier-v2",
        )


if __name__ == "__main__":
    unittest.main()
