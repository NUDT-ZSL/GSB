from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CLI = [sys.executable, str(ROOT / "cicache.py")]
SAMPLES = ROOT / "samples"


def run_cli(
    state: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [*CLI, "--state-dir", str(state), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=merged_env,
        timeout=timeout,
    )


def parse(proc: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI did not emit JSON (rc={proc.returncode})\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        ) from exc


def ok_cli(state: Path, *args: str) -> dict[str, Any]:
    proc = run_cli(state, *args)
    payload = parse(proc)
    if proc.returncode != 0 or not payload.get("ok"):
        raise AssertionError(
            f"CLI failed: {args}\nrc={proc.returncode}\n{json.dumps(payload, indent=2)}"
        )
    return payload


def error_cli(state: Path, *args: str) -> tuple[int, dict[str, Any]]:
    proc = run_cli(state, *args)
    return proc.returncode, parse(proc)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def load_sample(state: Path, name: str) -> dict[str, Any]:
    folder = SAMPLES / name
    return ok_cli(
        state,
        "load",
        "--tasks",
        str(folder / "tasks.json"),
        "--pipelines",
        str(folder / "pipelines.json"),
        "--config",
        str(folder / "config.json"),
    )


def task_record(
    name: str,
    *,
    size: int = 100,
    depends_on: list[str] | None = None,
    hit_count: int = 0,
    pinned: bool = False,
    written: int = 1000,
    last_hit: int = 1000,
) -> dict[str, Any]:
    return {
        "name": name,
        "depends_on": depends_on or [],
        "artifact_name": f"{name}.bin",
        "size_bytes": size,
        "written_at_ms": written,
        "last_hit_at_ms": last_hit,
        "hit_count": hit_count,
        "pinned": pinned,
    }


def config_record(
    version: int = 1,
    *,
    budget: int = 100000,
    caps: tuple[int, int, int] = (100000, 100000, 100000),
    tolerance: int = 150000,
    clock_tolerance: int = 0,
    threshold: int = 2,
) -> dict[str, Any]:
    return {
        "version": version,
        "tier_capacity_bytes": {
            "cold": caps[0],
            "warm": caps[1],
            "hot": caps[2],
        },
        "promotion_min_hits": threshold,
        "demotion_idle_ms": 1000,
        "eviction_batch_size": 2,
        "reclaim_byte_budget": budget,
        "fixed_point_scale": 1000,
        "comparison_tolerance": tolerance,
        "clock_rollback_tolerance_ms": clock_tolerance,
        "logical_clock_step_ms": 100,
    }


class EngineAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="cicache-test-"))
        self.state = self.temp / "state"

    def tearDown(self) -> None:
        shutil.rmtree(self.temp, ignore_errors=True)

    def test_promotion_demotion_and_pinned_retention(self) -> None:
        load_sample(self.state, "promotion")
        tiers = {}
        for run_seq in (1, 2, 3, 4):
            result = ok_cli(
                self.state, "run", "--pipeline", "release", "--run-seq", str(run_seq)
            )
            tiers[run_seq] = {
                task["task_name"]: (task["tier"], task["hit_count"])
                for task in result["tasks"]
            }
        self.assertEqual(tiers[1]["build"], ("cold", 1))
        self.assertEqual(tiers[2]["build"], ("warm", 2))
        self.assertEqual(tiers[3]["build"], ("hot", 3))
        self.assertEqual(tiers[4]["build"], ("hot", 4))

        keepalive = ok_cli(
            self.state, "run", "--pipeline", "keepalive", "--run-seq", "1"
        )
        demotions = {
            item["task_name"]: (item["from_tier"], item["to_tier"])
            for item in keepalive["transitions"]
        }
        self.assertEqual(demotions["build"], ("hot", "warm"))
        self.assertEqual(demotions["test"], ("hot", "warm"))
        self.assertNotIn("docs", demotions)

        build_only = ok_cli(
            self.state, "run", "--pipeline", "build_only", "--run-seq", "1"
        )
        demotions = {
            item["task_name"]: (item["from_tier"], item["to_tier"])
            for item in build_only["transitions"]
        }
        self.assertEqual(demotions["test"], ("warm", "cold"))
        self.assertEqual(demotions["package"], ("warm", "cold"))
        self.assertNotIn("docs", demotions)

        status = ok_cli(self.state, "status")
        artifacts = {item["task_name"]: item for item in status["artifacts"]}
        self.assertEqual(artifacts["docs"]["tier"], "hot")
        self.assertTrue(artifacts["docs"]["pinned"])
        verify = ok_cli(self.state, "verify")
        self.assertTrue(verify["ok"])
        self.assertEqual(verify["errors"], [])

    def test_eviction_budget_reclaim_and_idempotence(self) -> None:
        load_sample(self.state, "eviction")
        first = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertEqual(
            [(item["task_name"], item["reclaimed_bytes"]) for item in first["evictions"]],
            [("alpha", 600), ("beta", 600)],
        )
        self.assertEqual(first["reclaimed_bytes"], 1200)
        self.assertEqual(
            {item["tier"]: item["used_bytes"] for item in first["capacity"]},
            {"cold": 2000, "warm": 0, "hot": 0},
        )
        self.assertTrue(
            any(
                event["precision_sensitive"] and event["affects_selection"]
                for event in first["precision_events"]
            )
        )
        self.assertIn("error_a", first["precision_events"][0])
        self.assertIn("higher_scale_changes", first["precision_events"][0])

        status = ok_cli(self.state, "status")
        artifacts = {item["task_name"]: item for item in status["artifacts"]}
        self.assertTrue(artifacts["alpha"]["evicted"])
        self.assertTrue(artifacts["beta"]["evicted"])
        self.assertFalse(artifacts["keep"]["evicted"])
        self.assertEqual(
            artifacts["alpha"]["referencing_runs"],
            [
                {
                    "pipeline": "release",
                    "run_seq": 1,
                    "hit": True,
                    "reason": "hit",
                }
            ],
        )

        repeat = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(
            {item["task_name"]: item["hit_count"] for item in repeat["tasks"]},
            {"alpha": 1, "beta": 2, "gamma": 3, "delta": 4, "epsilon": 5, "keep": 1},
        )

        second = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "2"
        )
        second_tasks = {item["task_name"]: item for item in second["tasks"]}
        self.assertEqual(second_tasks["alpha"]["reason"], "evicted")
        self.assertFalse(second_tasks["alpha"]["hit"])
        self.assertEqual(second_tasks["alpha"]["hit_count"], 1)
        self.assertTrue(second_tasks["gamma"]["hit"])
        self.assertEqual(second_tasks["gamma"]["hit_count"], 4)
        self.assertEqual(ok_cli(self.state, "verify")["errors"], [])

    def test_cas_digest_reuse_and_object_identity(self) -> None:
        from pipeline_cache.cas import ObjectStore
        from pipeline_cache.model import artifact_digest, iter_artifact_bytes

        cas = ObjectStore(self.temp / "objects", self.temp / "tmp")
        digest = artifact_digest("build", 1000, 257)
        chunks = list(iter_artifact_bytes("build", 1000, 257))
        self.assertEqual(len(b"".join(chunks)), 257)
        first = cas.put_bytes(chunks, digest, 257)
        second = cas.put_bytes(chunks, digest, 257)
        self.assertEqual(first, (digest, 257))
        self.assertEqual(second, (digest, 257))
        files = [path for path in (self.temp / "objects").rglob("*") if path.is_file()]
        self.assertEqual(len(files), 1)
        data = files[0].read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
        self.assertEqual(cas.verify(digest, 257)["size_bytes"], 257)

    def test_invalid_task_and_digest_failure_leave_no_state(self) -> None:
        tasks = {
            "schema_version": 1,
            "tasks": [
                task_record("a", depends_on=["b"]),
                task_record("b", depends_on=["a"]),
            ],
        }
        pipelines = {
            "schema_version": 1,
            "pipelines": [
                {
                    "name": "p",
                    "tasks": ["a"],
                    "runs": [{"run_seq": 1, "at_ms": 1000}],
                }
            ],
        }
        write_json(self.temp / "tasks-bad.json", tasks)
        write_json(self.temp / "pipelines.json", pipelines)
        write_json(self.temp / "config.json", config_record())
        rc, payload = error_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks-bad.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config.json"),
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["error"]["code"], "E_BAD_TASK")
        self.assertFalse(self.state.exists())

        mismatch_tasks = {
            "schema_version": 1,
            "tasks": [
                {
                    **task_record("a"),
                    "expected_sha256": "0" * 64,
                }
            ],
        }
        write_json(self.temp / "tasks-mismatch.json", mismatch_tasks)
        rc, payload = error_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks-mismatch.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config.json"),
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["error"]["code"], "E_DIGEST_MISMATCH")
        self.assertFalse(self.state.exists())

    def test_clock_and_config_rollback(self) -> None:
        write_json(
            self.temp / "tasks.json",
            {"schema_version": 1, "tasks": [task_record("a")]},
        )
        write_json(
            self.temp / "pipelines.json",
            {
                "schema_version": 1,
                "pipelines": [
                    {
                        "name": "p",
                        "tasks": ["a"],
                        "runs": [
                            {"run_seq": 1, "at_ms": 2000},
                            {"run_seq": 2, "at_ms": 1000},
                        ],
                    }
                ],
            },
        )
        write_json(self.temp / "config-v2.json", config_record(2))
        ok_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config-v2.json"),
        )
        ok_cli(self.state, "run", "--pipeline", "p", "--run-seq", "1")
        rc, payload = error_cli(
            self.state, "run", "--pipeline", "p", "--run-seq", "2"
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["error"]["code"], "E_CLOCK_ROLLBACK")
        self.assertEqual(payload["error"]["fields"]["run_seq"], 2)
        status = ok_cli(self.state, "status")
        self.assertEqual([run["run_seq"] for run in status["runs"]], [1])

        write_json(self.temp / "config-v1.json", config_record(1))
        rc, payload = error_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config-v1.json"),
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["error"]["code"], "E_CONFIG_ROLLBACK")
        status = ok_cli(self.state, "status")
        self.assertEqual(status["config"]["version"], 2)

    def test_missing_task_and_runtime_digest_error_are_isolated(self) -> None:
        tasks = {
            "schema_version": 1,
            "tasks": [task_record("a", size=64), task_record("b", size=64)],
        }
        pipelines = {
            "schema_version": 1,
            "pipelines": [
                {
                    "name": "p",
                    "tasks": ["a", "ghost", "b"],
                    "runs": [{"run_seq": 1, "at_ms": 1000}],
                }
            ],
        }
        write_json(self.temp / "tasks.json", tasks)
        write_json(self.temp / "pipelines.json", pipelines)
        write_json(self.temp / "config.json", config_record())
        ok_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config.json"),
        )
        result = ok_cli(
            self.state, "run", "--pipeline", "p", "--run-seq", "1"
        )
        self.assertEqual(result["status"], "committed_with_errors")
        self.assertEqual([item["code"] for item in result["errors"]], ["E_TASK_MISSING"])
        self.assertEqual(result["errors"][0]["task_name"], "ghost")
        task_map = {item["task_name"]: item for item in result["tasks"]}
        self.assertTrue(task_map["a"]["hit"])
        self.assertTrue(task_map["b"]["hit"])

        status = ok_cli(self.state, "status")
        artifact_a = next(item for item in status["artifacts"] if item["task_name"] == "a")
        object_path = (
            self.state
            / "objects"
            / artifact_a["digest"][:2]
            / artifact_a["digest"]
        )
        original = object_path.read_bytes()
        object_path.write_bytes(b"corrupt" + original[7:])

        pipelines["pipelines"][0]["tasks"] = ["a", "b"]
        pipelines["pipelines"][0]["runs"].append({"run_seq": 2, "at_ms": 1100})
        write_json(self.temp / "pipelines-v2.json", pipelines)
        # Pipelines are part of the loaded snapshot; reset and reload the changed
        # pipeline for the runtime digest-isolation check.
        ok_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks.json"),
            "--pipelines",
            str(self.temp / "pipelines-v2.json"),
            "--config",
            str(self.temp / "config.json"),
            "--reset",
        )
        status = ok_cli(self.state, "status")
        artifact_a = next(item for item in status["artifacts"] if item["task_name"] == "a")
        object_path = (
            self.state
            / "objects"
            / artifact_a["digest"][:2]
            / artifact_a["digest"]
        )
        size = object_path.stat().st_size
        object_path.write_bytes(b"x" * size)
        result = ok_cli(
            self.state, "run", "--pipeline", "p", "--run-seq", "1"
        )
        self.assertEqual(result["status"], "committed_with_errors")
        self.assertEqual(
            {item["code"] for item in result["errors"]}, {"E_DIGEST_MISMATCH"}
        )
        task_map = {item["task_name"]: item for item in result["tasks"]}
        self.assertFalse(task_map["a"]["hit"])
        self.assertTrue(task_map["b"]["hit"])

    def test_evict_missing_candidate_and_budget_abandonment(self) -> None:
        load_sample(self.state, "eviction")
        rc, payload = error_cli(
            self.state, "evict", "--candidate", "does-not-exist"
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(payload["error"]["code"], "E_EVICT_CANDIDATE_MISSING")
        self.assertEqual(payload["error"]["fields"]["task_name"], "does-not-exist")
        status = ok_cli(self.state, "status")
        self.assertEqual(status["logical_clock_ms"], 0)
        self.assertEqual(status["reclaim_queue"], [])

        write_json(
            self.temp / "config-v2.json",
            config_record(2, budget=500, caps=(2000, 2000, 2000), threshold=10),
        )
        folder = SAMPLES / "eviction"
        ok_cli(
            self.state,
            "load",
            "--tasks",
            str(folder / "tasks.json"),
            "--pipelines",
            str(folder / "pipelines.json"),
            "--config",
            str(self.temp / "config-v2.json"),
        )
        result = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertEqual(result["evictions"], [])
        self.assertTrue(result["abandoned"])
        reasons = {item["reason"] for item in result["abandoned"]}
        self.assertIn("reclaim_budget_exhausted", reasons)
        self.assertIn("pinned_fixed_retention", reasons)
        self.assertEqual(result["unresolved"][0]["tier"], "cold")
        self.assertGreater(result["unresolved"][0]["over_by"], 0)
        self.assertEqual(result["reclaimed_bytes"], 0)

    def test_eviction_prefers_idle_before_hit_count(self) -> None:
        tasks = {
            "schema_version": 1,
            "tasks": [
                task_record("old_high", size=600, hit_count=100),
                task_record("young_low", size=600, hit_count=0, last_hit=1900),
            ],
        }
        pipelines = {
            "schema_version": 1,
            "pipelines": [
                {
                    "name": "p",
                    "tasks": ["old_high"],
                    "runs": [{"run_seq": 1, "at_ms": 2000}],
                }
            ],
        }
        write_json(self.temp / "tasks.json", tasks)
        write_json(self.temp / "pipelines.json", pipelines)
        write_json(
            self.temp / "config.json",
            config_record(caps=(600, 600, 600), threshold=1000),
        )
        ok_cli(
            self.state,
            "load",
            "--tasks",
            str(self.temp / "tasks.json"),
            "--pipelines",
            str(self.temp / "pipelines.json"),
            "--config",
            str(self.temp / "config.json"),
        )
        result = ok_cli(self.state, "evict", "--at-ms", "2000")
        self.assertEqual(
            [item["task_name"] for item in result["evicted"]], ["old_high"]
        )
        self.assertEqual(result["evicted"][0]["details"]["idle_ms"], 1000)

    def test_judge_is_stable_and_read_only(self) -> None:
        load_sample(self.state, "eviction")
        preview = ok_cli(
            self.state, "judge", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertFalse(preview["committed"])
        self.assertEqual(
            [item["task_name"] for item in preview["tasks"]],
            sorted(item["task_name"] for item in preview["tasks"]),
        )
        for item in preview["tasks"]:
            self.assertIn("hit", item)
            self.assertIn("tier", item)
            self.assertIn("error_bound", item)
            self.assertIn("precision_sensitive", item)
        status = ok_cli(self.state, "status")
        self.assertEqual(status["runs"], [])
        self.assertEqual({item["hit_count"] for item in status["artifacts"]}, {0, 1, 2, 3, 4})

        ok_cli(self.state, "run", "--pipeline", "release", "--run-seq", "1")
        committed = ok_cli(
            self.state, "judge", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertTrue(committed["committed"])
        self.assertEqual(
            [item["task_name"] for item in committed["tasks"]],
            sorted(item["task_name"] for item in committed["tasks"]),
        )

    def test_crash_after_commit_then_resume(self) -> None:
        load_sample(self.state, "eviction")
        proc = run_cli(
            self.state,
            "run",
            "--pipeline",
            "release",
            "--run-seq",
            "1",
            env={"CI_CACHE_TEST_CRASH_AT": "after_run_commit"},
        )
        self.assertNotEqual(proc.returncode, 0)
        status = ok_cli(self.state, "status")
        self.assertEqual(len(status["runs"]), 1)
        self.assertEqual(status["runs"][0]["status"], "committed")
        artifacts = {item["task_name"]: item for item in status["artifacts"]}
        self.assertEqual(artifacts["alpha"]["hit_count"], 1)
        self.assertTrue(artifacts["alpha"]["evicted"])
        self.assertEqual(
            {item["status"] for item in status["reclaim_queue"]}, {"pending"}
        )

        resumed = ok_cli(self.state, "resume")
        self.assertEqual(resumed["pending_reclaims"], 0)
        self.assertEqual(
            sum(item["reclaimed_bytes"] for item in resumed["reclaimed"]), 1200
        )
        status = ok_cli(self.state, "status")
        self.assertEqual(status["reclaim_queue"][0]["status"], "reclaimed")
        self.assertEqual(ok_cli(self.state, "verify")["errors"], [])
        repeat = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertTrue(repeat["idempotent"])
        self.assertEqual(
            {item["task_name"]: item["hit_count"] for item in repeat["tasks"]}["alpha"],
            1,
        )

    def test_crash_mid_transaction_rolls_back_and_can_retry(self) -> None:
        load_sample(self.state, "promotion")
        proc = run_cli(
            self.state,
            "run",
            "--pipeline",
            "release",
            "--run-seq",
            "1",
            env={"CI_CACHE_TEST_CRASH_AT": "mid_transaction"},
        )
        self.assertNotEqual(proc.returncode, 0)
        status = ok_cli(self.state, "status")
        self.assertEqual(status["runs"], [])
        self.assertEqual({item["hit_count"] for item in status["artifacts"]}, {0})
        self.assertEqual(status["logical_clock_ms"], 0)
        ok_cli(self.state, "resume")
        result = ok_cli(
            self.state, "run", "--pipeline", "release", "--run-seq", "1"
        )
        self.assertEqual({item["hit_count"] for item in result["tasks"]}, {1})

    def test_crash_after_object_unlink_recovers_accounting(self) -> None:
        load_sample(self.state, "eviction")
        proc = run_cli(
            self.state,
            "run",
            "--pipeline",
            "release",
            "--run-seq",
            "1",
            env={"CI_CACHE_TEST_CRASH_AT": "after_object_delete"},
        )
        self.assertNotEqual(proc.returncode, 0)
        status = ok_cli(self.state, "status")
        pending = [item for item in status["reclaim_queue"] if item["status"] == "pending"]
        self.assertTrue(pending)
        resumed = ok_cli(self.state, "resume")
        self.assertEqual(
            sum(item["reclaimed_bytes"] for item in resumed["reclaimed"]), 1200
        )
        self.assertEqual(ok_cli(self.state, "verify")["errors"], [])
        status = ok_cli(self.state, "status")
        self.assertEqual(
            {item["status"] for item in status["reclaim_queue"]}, {"reclaimed"}
        )

    def test_resume_rematerializes_missing_active_object(self) -> None:
        load_sample(self.state, "promotion")
        status = ok_cli(self.state, "status")
        artifact = next(item for item in status["artifacts"] if item["task_name"] == "build")
        path = self.state / "objects" / artifact["digest"][:2] / artifact["digest"]
        path.unlink()
        resumed = ok_cli(self.state, "resume")
        self.assertTrue(
            any(
                item["task_name"] == "build" and item["recovered"]
                for item in resumed["objects_recovered"]
            )
        )
        self.assertTrue(path.is_file())
        self.assertTrue(ok_cli(self.state, "verify")["ok"])

    def test_concurrent_same_run_is_committed_once(self) -> None:
        load_sample(self.state, "promotion")
        commands = [
            [*CLI, "--state-dir", str(self.state), "run", "--pipeline", "release", "--run-seq", "1"]
            for _ in range(4)
        ]
        processes = [
            subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for command in commands
        ]
        payloads = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            payloads.append(json.loads(stdout))
        self.assertTrue(all(payload["ok"] for payload in payloads))
        self.assertEqual(
            sum(1 for payload in payloads if not payload["idempotent"]), 1
        )
        status = ok_cli(self.state, "status")
        self.assertEqual(len(status["runs"]), 1)
        self.assertEqual({item["hit_count"] for item in status["artifacts"]}, {1})

    def test_verify_detects_tampered_active_object(self) -> None:
        load_sample(self.state, "promotion")
        status = ok_cli(self.state, "status")
        artifact = status["artifacts"][0]
        path = (
            self.state / "objects" / artifact["digest"][:2] / artifact["digest"]
        )
        size = path.stat().st_size
        path.write_bytes(b"z" * size)
        verify = parse(run_cli(self.state, "verify"))
        self.assertFalse(verify["ok"])
        self.assertIn("E_DIGEST_MISMATCH", {item["code"] for item in verify["errors"]})


if __name__ == "__main__":
    unittest.main()
