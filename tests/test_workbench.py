from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from transformer_diag.cache import ContentAddressedCache
from transformer_diag.engine import Engine
from transformer_diag.errors import BusinessError
from transformer_diag.input_store import InputStore
from transformer_diag.locking import NonBlockingCommitLock
from transformer_diag.models import JobSpec, RuntimeConfig
from transformer_diag.sample import SAMPLE_BATCH_1, write_sample_files
from transformer_diag.selfcheck import run_selfcheck
from transformer_diag.snapshots import SnapshotStore
from transformer_diag.util import atomic_write_json, read_json
from transformer_diag.workspace import Workspace


class WorkbenchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = Path(tempfile.mkdtemp(prefix="td-workbench-test-"))
        self.root = self.tempdir / "workspace"
        self.root.mkdir(parents=True)
        self.samples = write_sample_files(self.root / "samples")

    def tearDown(self) -> None:
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def run_jobs(self, path: str | Path, **engine_kwargs: object) -> dict:
        return Engine(self.root, **engine_kwargs).run(path)

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        atomic_write_json(path, value, pretty=True)
        return path

    def test_incremental_reuse_revisions_dtype_mutation_and_byte_identity(self) -> None:
        initial = self.run_jobs(self.samples["batch_1"])
        self.assertEqual(initial["status"], "ok")
        self.assertEqual(len(initial["invalidation"]["recomputed_nodes"]), 8)
        self.assertEqual(initial["errors"], [])
        self.assertTrue(initial["consistency"]["all_byte_identical"])

        repeat = self.run_jobs(self.samples["batch_1"])
        self.assertEqual(repeat["invalidation"]["recomputed_nodes"], [])
        self.assertEqual(len(repeat["invalidation"]["reused_nodes"]), 8)
        self.assertEqual(repeat["invalidation"]["affected_jobs"], [])

        nodes = read_json(Workspace(self.root).paths.nodes)["nodes"]
        required = {
            "input_hash",
            "op_kind",
            "input_shape",
            "block_size",
            "axis",
            "eps",
            "dtype",
            "upstream_hash",
            "policy_signature",
        }
        for job_id in ("softmax_b1", "layernorm_b1"):
            material = nodes[job_id]["block_stats"]["key_material"]
            self.assertTrue(required.issubset(material))
            self.assertIsNone(nodes[job_id]["validate_plan"]["key_material"]["upstream_hash"])
            self.assertEqual(
                material["upstream_hash"], nodes[job_id]["validate_plan"]["content_hash"]
            )
            self.assertEqual(
                nodes[job_id]["normalize"]["key_material"]["upstream_hash"],
                nodes[job_id]["block_stats"]["content_hash"],
            )
            self.assertEqual(
                nodes[job_id]["output"]["key_material"]["upstream_hash"],
                nodes[job_id]["normalize"]["content_hash"],
            )

        appended = self.run_jobs(self.samples["jobs_full"])
        self.assertEqual(
            appended["invalidation"]["affected_jobs"],
            ["layernorm_b2", "softmax_b2"],
        )
        self.assertTrue(
            {"softmax_b1", "layernorm_b1"}.issubset(
                set(appended["invalidation"]["reused_jobs"])
            )
        )
        self.assertEqual(appended["errors"], [])

        revised = self.run_jobs(self.samples["batch_2_revision"])
        self.assertEqual(revised["invalidation"]["affected_jobs"], ["softmax_b2"])
        self.assertEqual(revised["invalidation"]["recomputed_jobs"], ["softmax_b2"])
        self.assertTrue(
            {"softmax_b1", "layernorm_b1", "layernorm_b2"}.issubset(
                set(revised["invalidation"]["reused_jobs"])
            )
        )

        dtype_job = dict(SAMPLE_BATCH_1[1])
        dtype_job.update({"revision": 2, "dtype": "float64"})
        dtype_file = self.root / "dtype_bump.jsonl"
        dtype_file.write_text(json.dumps(dtype_job) + "\n", encoding="utf-8")
        dtype_result = self.run_jobs(dtype_file)
        self.assertEqual(dtype_result["invalidation"]["affected_jobs"], ["layernorm_b1"])
        self.assertEqual(dtype_result["invalidation"]["recomputed_jobs"], ["layernorm_b1"])
        self.assertTrue(
            {"softmax_b1", "layernorm_b2", "softmax_b2"}.issubset(
                set(dtype_result["invalidation"]["reused_jobs"])
            )
        )

        workspace = Workspace(self.root)
        registry = workspace.load_jobs_registry()
        target = JobSpec.from_dict(registry["layernorm_b2"])
        before_mutation = read_json(workspace.paths.run_state)["jobs"]["layernorm_b2"]
        mutation = InputStore(workspace).mutate_flat(target, 0, 0.125)
        mutated = self.run_jobs(self.samples["batch_2_revision"])
        self.assertEqual(mutated["invalidation"]["affected_jobs"], ["layernorm_b2"])
        self.assertEqual(mutated["invalidation"]["recomputed_jobs"], ["layernorm_b2"])
        self.assertNotEqual(mutation["before_hash"], mutation["after_hash"])
        self.assertNotEqual(before_mutation["input_hash"], mutation["after_hash"])
        self.assertTrue(mutated["consistency"]["all_byte_identical"])

        selfcheck = run_selfcheck(workspace)
        self.assertTrue(selfcheck["ok"], selfcheck["failed_checks"])

        final_jobs = self.root / "final_jobs.jsonl"
        final_jobs.write_text(
            "".join(
                json.dumps(item, sort_keys=True) + "\n"
                for item in workspace.load_jobs_registry().values()
            ),
            encoding="utf-8",
        )
        fresh_root = self.tempdir / "fresh-full"
        fresh_workspace = Workspace(fresh_root)
        fresh_workspace.initialize(reset=True)
        shutil.copy2(workspace.paths.input_meta, fresh_workspace.paths.input_meta)
        shutil.copytree(workspace.paths.inputs, fresh_workspace.paths.inputs, dirs_exist_ok=True)
        fresh = Engine(fresh_root).run(final_jobs)
        self.assertEqual(fresh["status"], "ok")
        for job_id in workspace.load_jobs_registry():
            incremental_bytes = (
                workspace.paths.outputs / f"{job_id}.npy"
            ).read_bytes()
            full_bytes = (fresh_workspace.paths.outputs / f"{job_id}.npy").read_bytes()
            self.assertEqual(
                incremental_bytes,
                full_bytes,
                f"incremental and clean full output differ for {job_id}",
            )
        self.assertTrue(run_selfcheck(fresh_workspace)["ok"])

    def test_validation_business_codes_and_fields(self) -> None:
        base = dict(SAMPLE_BATCH_1[0])
        cases = [
            ({**base, "op_kind": "matmul"}, "E_OP_UNSUPPORTED", "op_kind"),
            ({**base, "axis": 2}, "E_AXIS_INVALID", "axis"),
            ({**base, "eps": 0.0}, "E_EPS_INVALID", "eps"),
            ({**base, "eps": -1e-6}, "E_EPS_INVALID", "eps"),
            ({**base, "dtype": "int32"}, "E_DTYPE_UNSUPPORTED", "dtype"),
        ]
        for raw, code, field in cases:
            with self.assertRaises(BusinessError) as context:
                JobSpec.from_dict(raw)
            self.assertEqual(context.exception.code, code)
            self.assertEqual(context.exception.job_id, base["job_id"])
            self.assertEqual(context.exception.field, field)

        invalid = self.root / "invalid.jsonl"
        invalid.write_text(json.dumps({**base, "axis": 9}) + "\n", encoding="utf-8")
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "transformer_diag",
                "--workspace",
                str(self.root),
                "run",
                "--jobs",
                str(invalid),
            ],
            text=True,
            capture_output=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        self.assertNotEqual(process.returncode, 0)
        payload = json.loads(process.stdout)
        self.assertEqual(payload["code"], "E_AXIS_INVALID")
        self.assertEqual(payload["job_id"], base["job_id"])
        self.assertEqual(payload["field"], "axis")

    def test_tight_tolerance_reports_stat_and_element_positions(self) -> None:
        config = self.write_json(
            "tight_config.json",
            {
                "tolerance": {
                    "atol": 0.0,
                    "rtol": 0.0,
                    "stat_atol": 0.0,
                    "stat_rtol": 0.0,
                }
            },
        )
        report = self.run_jobs(self.samples["jobs_full"], config_path=config)
        self.assertEqual(report["status"], "failed")
        codes = {error["code"] for error in report["errors"]}
        self.assertIn("E_TOLERANCE_EXCEEDED", codes)
        self.assertIn("E_STAT_MISMATCH", codes)
        tolerance_errors = [
            error for error in report["errors"] if error["code"] == "E_TOLERANCE_EXCEEDED"
        ]
        self.assertTrue(tolerance_errors)
        details = tolerance_errors[0]["details"]
        self.assertIn("max_abs", details)
        self.assertIn("max_rel", details)
        self.assertIn("max_abs_position", details)
        self.assertIn("exceed_positions", details)
        self.assertIn("spectrum", details)

    def test_policy_band_guard_and_subnormal_protection(self) -> None:
        job = dict(SAMPLE_BATCH_1[0])
        job.update({"job_id": "guarded", "eps": 9.5e-6, "dtype": "float32"})
        jobs_file = self.root / "guarded.jsonl"
        jobs_file.write_text(json.dumps(job) + "\n", encoding="utf-8")
        config = self.write_json(
            "band_config.json",
            {
                "policy": {
                    "strategy": "band",
                    "band": 0.1,
                    "eps_min": 1e-5,
                    "block_size_max": 1048576,
                    "denom_min": 1e-300,
                    "subnormal_policy": "flush",
                }
            },
        )
        report = self.run_jobs(jobs_file, config_path=config)
        self.assertEqual(report["status"], "ok")
        plan_node = read_json(Workspace(self.root).paths.nodes)["nodes"]["guarded"][
            "validate_plan"
        ]
        cached = ContentAddressedCache(Workspace(self.root)).get(
            plan_node["cache_key"], expected_job_id="guarded", expected_stage="validate_plan"
        )
        plan = cached["payload"]["value"]
        self.assertTrue(plan["guards"]["eps"]["intervened"])
        self.assertEqual(plan["guards"]["eps"]["effective_eps"], 1e-5)

        workspace = Workspace(self.root)
        spec = JobSpec.from_dict(workspace.load_jobs_registry()["guarded"])
        InputStore(workspace).mutate_flat(spec, 0, 1e-40)
        report = self.run_jobs(jobs_file, config_path=config)
        plan_node = read_json(workspace.paths.nodes)["nodes"]["guarded"]["validate_plan"]
        cached = ContentAddressedCache(workspace).get(
            plan_node["cache_key"], expected_job_id="guarded", expected_stage="validate_plan"
        )
        plan = cached["payload"]["value"]
        self.assertGreaterEqual(plan["guards"]["subnormal"]["count"], 1)
        self.assertIn([0, 0], plan["guards"]["subnormal"]["positions"])
        self.assertGreaterEqual(plan["guards"]["intervention_count"], 2)

        strict_config = self.write_json(
            "strict_config.json",
            {
                "policy": {
                    "strategy": "strict",
                    "band": 1e-6,
                    "eps_min": 1e-5,
                    "block_size_max": 1048576,
                    "denom_min": 1e-300,
                    "subnormal_policy": "flush",
                }
            },
        )
        strict_report = self.run_jobs(jobs_file, config_path=strict_config)
        self.assertEqual(strict_report["status"], "failed")
        self.assertIn(
            "E_POLICY_GUARD",
            {error["code"] for error in strict_report["errors"]},
        )

        with self.assertRaises(BusinessError) as context:
            RuntimeConfig.from_dict({"tolerance": {"atol": True}})
        self.assertEqual(context.exception.code, "E_POLICY_INVALID")
        with self.assertRaises(BusinessError) as context:
            RuntimeConfig.from_dict({"policy": {"strategy": "sometimes"}})
        self.assertEqual(context.exception.code, "E_POLICY_INVALID")

    def test_snapshot_rollback_whole_replace_and_corruption(self) -> None:
        self.run_jobs(self.samples["batch_1"])
        workspace = Workspace(self.root)
        store = SnapshotStore(workspace)
        current = store.read("tolerance", "global")
        self.assertIsNotNone(current)
        assert current is not None
        current_version = current["version"]
        replacement = store.put(
            "tolerance",
            "global",
            current_version + 1,
            {
                "atol": 1e-7,
                "rtol": 1e-7,
                "stat_atol": 1e-8,
                "stat_rtol": 1e-8,
            },
        )
        self.assertEqual(replacement["replacement"], "atomic-whole")
        self.assertEqual(replacement["previous_version"], current_version)
        with self.assertRaises(BusinessError) as context:
            store.put("tolerance", "global", current_version, {"atol": 1.0})
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")
        with self.assertRaises(BusinessError) as context:
            store.put(
                "tolerance",
                "global",
                current_version + 1,
                {"different": "content"},
            )
        self.assertEqual(context.exception.code, "E_SNAPSHOT_ROLLBACK")

        path = store._path("tolerance", "global")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["version"] = data["version"] + 100
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(BusinessError) as context:
            store.read("tolerance", "global")
        self.assertEqual(context.exception.code, "E_CACHE_CORRUPT")

    def test_cache_corruption_is_attributed(self) -> None:
        report = self.run_jobs(self.samples["batch_1"])
        node = report["jobs"]["softmax_b1"]["nodes"]["validate_plan"]
        workspace = Workspace(self.root)
        object_path = workspace.paths.cache_objects / f"{node['content_hash']}.bin"
        object_path.write_bytes(b"corrupted")
        corrupted = self.run_jobs(self.samples["batch_1"])
        self.assertEqual(corrupted["status"], "failed")
        self.assertIn(
            "E_CACHE_CORRUPT",
            {error["code"] for error in corrupted["errors"]},
        )
        failed_node = corrupted["jobs"]["softmax_b1"]["nodes"]["validate_plan"]
        self.assertEqual(failed_node["status"], "failed")
        self.assertEqual(failed_node["error_code"], "E_CACHE_CORRUPT")

    def test_injection_all_four_stages_degradation_chain(self) -> None:
        downstream = {
            "validate_plan": {"block_stats", "normalize", "output"},
            "block_stats": {"normalize", "output"},
            "normalize": {"output"},
            "output": set(),
        }
        for stage in ("validate_plan", "block_stats", "normalize", "output"):
            stage_root = self.tempdir / f"inject-{stage}"
            StageRootWorkspace = Workspace(stage_root)
            StageRootWorkspace.initialize(reset=True)
            stage_samples = write_sample_files(stage_root / "samples")
            report = Engine(
                stage_root,
                injections=[f"{stage}:E_INJECT_{stage.upper()}:softmax_b1"],
                degrade_policy="skip",
            ).run(stage_samples["batch_1"])
            self.assertEqual(report["status"], "degraded")
            self.assertEqual(len(report["injection_records"]), 1)
            injection = report["injection_records"][0]
            self.assertEqual(injection["stage"], stage)
            self.assertEqual(injection["job_id"], "softmax_b1")
            self.assertEqual(
                {chain["stage"] for chain in report["degradation_chains"]},
                downstream[stage],
            )
            for chain in report["degradation_chains"]:
                self.assertEqual(chain["upstream_code"], injection["code"])
                self.assertEqual(chain["upstream_stage"], stage)
                self.assertEqual(chain["category"], "skip")

    def test_unattributed_exception_is_recorded(self) -> None:
        engine = Engine(self.root)

        def explode(*args: object, **kwargs: object) -> dict:
            raise RuntimeError("synthetic unattributed failure")

        engine._compute_stage = explode  # type: ignore[method-assign]
        report = engine.run(self.samples["batch_1"])
        self.assertEqual(report["status"], "failed")
        unattributed = [
            error for error in report["errors"] if error["code"] == "E_UNATTRIBUTED"
        ]
        self.assertTrue(unattributed)
        self.assertEqual(unattributed[0]["stage"], "validate_plan")
        self.assertIn("details", unattributed[0])
        self.assertEqual(
            report["jobs"]["softmax_b1"]["nodes"]["validate_plan"]["status"], "failed"
        )

    def test_nonblocking_busy(self) -> None:
        workspace = Workspace(self.root)
        lock = NonBlockingCommitLock(workspace)
        lock.acquire()
        try:
            started = time.monotonic()
            with self.assertRaises(BusinessError) as context:
                self.run_jobs(self.samples["batch_1"])
            elapsed = time.monotonic() - started
        finally:
            lock.release()
        self.assertEqual(context.exception.code, "E_STAGE_BUSY")
        self.assertLess(elapsed, 0.5)

    def test_revision_stale_and_external_input_file_change(self) -> None:
        self.run_jobs(self.samples["batch_1"])
        workspace = Workspace(self.root)
        stale = dict(SAMPLE_BATCH_1[0])
        stale["block_size"] = 3
        stale_file = self.root / "stale.jsonl"
        stale_file.write_text(json.dumps(stale) + "\n", encoding="utf-8")
        with self.assertRaises(BusinessError) as context:
            self.run_jobs(stale_file)
        self.assertEqual(context.exception.code, "E_REVISION_STALE")
        self.assertEqual(context.exception.job_id, "softmax_b1")

        path = workspace.paths.inputs / "layernorm_b1.npy"
        array = np.load(path, allow_pickle=False)
        array.reshape(-1)[1] = np.asarray(1.75, dtype=array.dtype)
        np.save(path, array, allow_pickle=False)
        changed = self.run_jobs(self.samples["batch_1"])
        self.assertEqual(changed["invalidation"]["affected_jobs"], ["layernorm_b1"])
        self.assertEqual(changed["invalidation"]["recomputed_jobs"], ["layernorm_b1"])
        self.assertIn(
            "softmax_b1", changed["invalidation"]["reused_jobs"]
        )

    def test_selfcheck_detects_tampered_deviation_summary(self) -> None:
        self.run_jobs(self.samples["batch_1"])
        workspace = Workspace(self.root)
        self.assertTrue(run_selfcheck(workspace)["ok"])
        report = read_json(workspace.paths.latest_report)
        report["jobs"]["softmax_b1"]["diagnostics"]["deviation"]["max_abs"] += 1.0
        atomic_write_json(workspace.paths.latest_report, report, pretty=True)
        tampered = run_selfcheck(workspace)
        self.assertFalse(tampered["ok"])
        self.assertEqual(tampered["code"], "E_INCONSISTENT")
        self.assertIn(
            "deviation_spectrum_recomputed_from_detail",
            tampered["failed_checks"],
        )


if __name__ == "__main__":
    unittest.main()

