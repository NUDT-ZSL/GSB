from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

import generate_samples  # noqa: E402
from gitops_controller.controller import Controller  # noqa: E402
from gitops_controller.engine import rollback, resume  # noqa: E402
from gitops_controller.errors import GitOpsError  # noqa: E402
from gitops_controller.project import Project, default_project_path  # noqa: E402
from gitops_controller.util import canonical_json, json_text  # noqa: E402


def _copy_project(destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "samples"
    shutil.copytree(
        REPOSITORY_ROOT / "samples",
        target,
        ignore=shutil.ignore_patterns("runtime", "controller.db*", "*.lock"),
    )
    # The source cluster file is mutable during interactive use. Always restore
    # the deterministic seed state for isolated tests.
    (target / "cluster-state.json").write_text(
        json_text(generate_samples._cluster_state()),
        encoding="utf-8",
    )
    runtime = target / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    return target / "project.json"


@contextmanager
def project_fixture() -> Iterator[tuple[Path, Project]]:
    with tempfile.TemporaryDirectory(prefix="gitops-controller-test-") as temporary:
        project_path = _copy_project(Path(temporary))
        project = Project.load(project_path)
        yield project_path, project


def run_cli(
    project_path: Path,
    *arguments: str,
    expected_code: int = 0,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            "gitops_controller",
            "--project",
            str(project_path),
            *arguments,
        ],
        cwd=str(REPOSITORY_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if process.returncode != expected_code:
        raise AssertionError(
            f"CLI exited {process.returncode}, expected {expected_code}\n"
            f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
        )
    return process, json.loads(process.stdout) if process.stdout.strip() else {}


class ManifestAndMergeTests(unittest.TestCase):
    def test_merge_union_provenance_and_tie_break(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            controller.load()
            desired = controller.store.get_desired("staging")
            assert desired is not None
            names = {entry["app"] for entry in desired["entries"]}
            self.assertEqual(
                names,
                {
                    "analytics-api",
                    "catalog-api",
                    "checkout-api",
                    "identity-api",
                    "legacy-reports",
                    "mailer",
                    "postgres",
                    "web-frontend",
                    "worker",
                },
            )
            checkout = next(
                entry for entry in desired["entries"] if entry["app"] == "checkout-api"
            )
            self.assertEqual(checkout["source_format"], "v2")
            self.assertEqual(checkout["decision"]["reason"], "source_format_priority_v2")
            self.assertEqual(len(checkout["decision"]["candidates"]), 2)
            mailer = next(
                entry for entry in desired["entries"] if entry["app"] == "mailer"
            )
            self.assertEqual(mailer["source_format"], "v1")
            self.assertEqual(
                mailer["raw_time"],
                {"kind": "relative_seconds", "value": 300},
            )
            self.assertEqual(
                mailer["absolute_effective_time_ms"],
                generate_samples.V1_CLOCK_MS + 300_000,
            )
            self.assertEqual(mailer["time_semantics"]["scale_ms_per_unit"], 1000)
            self.assertEqual(mailer["time_semantics"]["rounding"], "floor")
            worker = next(
                entry for entry in desired["entries"] if entry["app"] == "worker"
            )
            self.assertEqual(worker["source_format"], "v2")
            self.assertEqual(
                worker["absolute_effective_time_ms"],
                worker["raw_time"]["value"],
            )
            self.assertEqual(worker["time_semantics"]["scale_ms_per_unit"], 1)
            self.assertEqual(worker["time_semantics"]["rounding"], "none")

    def test_repeated_merge_is_byte_stable(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            controller.load()
            first = {
                environment: controller.store.get_desired(environment)
                for environment in project.environments
            }
            controller.load()
            second = {
                environment: controller.store.get_desired(environment)
                for environment in project.environments
            }
            for environment in project.environments:
                self.assertEqual(
                    canonical_json(first[environment]),
                    canonical_json(second[environment]),
                )
            self.assertEqual(
                controller.store.migration_count("dev"),
                len(first["dev"]["migration_decisions"]),
            )
            connection = sqlite3.connect(str(project.runtime_db))
            try:
                merge_count = connection.execute(
                    "SELECT COUNT(*) FROM merges"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(merge_count, len(project.environments))

    def test_v1_invalid_version_and_cycle(self) -> None:
        with project_fixture() as (_, project):
            path = project.v1_manifest
            original = path.read_text(encoding="utf-8")
            data = json.loads(original)
            data["applications"]["identity-api"]["image_version"] = "not-semver"
            path.write_text(json_text(data), encoding="utf-8")
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_BAD_VERSION")

        with project_fixture() as (_, project):
            path = project.v1_manifest
            data = json.loads(path.read_text(encoding="utf-8"))
            data["applications"]["postgres"]["params"]["depends_on"] = ["mailer"]
            path.write_text(json_text(data), encoding="utf-8")
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_DEP_CYCLE")

    def test_v1_duplicate_json_application_key_is_rejected(self) -> None:
        with project_fixture() as (_, project):
            path = project.v1_manifest
            text = path.read_text(encoding="utf-8")
            needle = '"catalog-api": {'
            duplicate = (
                '"catalog-api": {"effective_seconds": 0, "image_version": "9.9.9", '
                '"params": {}, "replicas": 1},\n    ' + needle
            )
            path.write_text(text.replace(needle, duplicate, 1), encoding="utf-8")
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_BAD_MANIFEST")
            self.assertIn("duplicate JSON key", caught.exception.message)

    def test_v2_hash_generation_and_absolute_millis_validation(self) -> None:
        with project_fixture() as (_, project):
            shard = project.v2_shards_dir / "dev.json"
            with shard.open("ab") as handle:
                handle.write(b"\n")
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_BAD_MANIFEST")
            self.assertIn("hash", caught.exception.message)

        with project_fixture() as (_, project):
            connection = sqlite3.connect(str(project.v2_metadata))
            try:
                connection.execute(
                    """
                    UPDATE manifest_revisions SET generation = 9
                    WHERE environment = 'dev' AND sequence = 2
                    """
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_BAD_MANIFEST")
            self.assertIn("generation", caught.exception.message)

        with project_fixture() as (_, project):
            shard = project.v2_shards_dir / "dev.json"
            data = json.loads(shard.read_text(encoding="utf-8"))
            data["applications"][0]["effective_at_ms"] = 1.5
            shard.write_text(json_text(data), encoding="utf-8")
            digest = hashlib.sha256(shard.read_bytes()).hexdigest()
            connection = sqlite3.connect(str(project.v2_metadata))
            try:
                connection.execute(
                    """
                    UPDATE manifest_revisions SET shard_sha256 = ?
                    WHERE environment = 'dev'
                      AND sequence = (
                          SELECT MAX(sequence) FROM manifest_revisions
                          WHERE environment = 'dev'
                      )
                    """,
                    (digest,),
                )
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(GitOpsError) as caught:
                Controller(project).load()
            self.assertEqual(caught.exception.code, "E_BAD_MANIFEST")
            self.assertIn("effective_at_ms", caught.exception.message)


class PlanningTests(unittest.TestCase):
    def test_plan_is_stable_and_upstream_first(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            first = controller.plan("dev")
            second = controller.plan("dev")
            self.assertEqual(canonical_json(first), canonical_json(second))
            sequences = {op["app"]: op["sequence"] for op in first["operations"]}
            for operation in first["operations"]:
                for upstream in operation["upstream_rollback_condition"]["upstreams"]:
                    upstream_sequence = upstream["operation_sequence"]
                    if upstream_sequence is not None:
                        self.assertLess(upstream_sequence, operation["sequence"])
                self.assertEqual(
                    operation["target"]["manifest_revision"],
                    operation["version_check"]["target_manifest_revision"],
                )
            self.assertIn("legacy-reports", sequences)
            delete_operation = next(
                op for op in first["operations"] if op["app"] == "legacy-reports"
            )
            self.assertEqual(delete_operation["trigger"]["kind"], "manifest_tombstone")

    def test_version_rollback_is_rejected(self) -> None:
        with project_fixture() as (project_path, _):
            process, payload = run_cli(
                project_path,
                "plan",
                "--env",
                "staging",
                expected_code=2,
            )
            self.assertEqual(payload["error"]["code"], "E_VERSION_ROLLBACK")
            self.assertEqual(payload["error"]["details"]["app"], "checkout-api")
            self.assertEqual(
                payload["error"]["details"]["committed_manifest_revision"], 14
            )
            self.assertEqual(process.stderr, "")


class ExecutionTests(unittest.TestCase):
    def test_budget_exhaustion_and_resume(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            result = rollback(
                controller,
                "dev",
                budget_ms=500,
                operation_cost_ms=250,
            )
            run_result = result["run"]
            self.assertEqual(run_result["status"], "partial")
            self.assertEqual(run_result["operation_counts"]["committed"], 2)
            self.assertEqual(run_result["operation_counts"]["not_started"], 6)
            self.assertEqual(
                run_result["clock_end_ms"],
                generate_samples.V1_CLOCK_MS + 500,
            )
            for operation in run_result["operations"][2:]:
                self.assertFalse(operation["started"])
                self.assertEqual(operation["outcome"]["status"], "not_started")
                self.assertEqual(operation["outcome"]["reason"], "budget_exhausted")
                self.assertIsNone(operation["logical_start_ms"])
            resumed = resume(
                controller,
                "dev",
                budget_ms=10_000,
                operation_cost_ms=250,
            )
            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["run"]["status"], "complete")
            self.assertTrue(resumed["converged"])

    def test_failed_after_start_blocks_dependents(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            result = rollback(
                controller,
                "dev",
                budget_ms=10_000,
                operation_cost_ms=250,
                fail_apps={"postgres"},
            )
            operations = {
                operation["app"]: operation for operation in result["run"]["operations"]
            }
            postgres = operations["postgres"]
            self.assertEqual(postgres["status"], "failed")
            self.assertTrue(postgres["started"])
            self.assertEqual(postgres["outcome"]["status"], "failed_after_start")
            self.assertEqual(
                postgres["logical_end_ms"] - postgres["logical_start_ms"], 250
            )
            self.assertEqual(operations["analytics-api"]["status"], "not_started")
            self.assertIn(
                "upstream_not_committed:postgres",
                operations["analytics-api"]["reason"],
            )
            self.assertFalse(operations["analytics-api"]["started"])
            self.assertEqual(result["run"]["status"], "failed")

    def test_active_abandonment_is_distinct(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            result = rollback(
                controller,
                "dev",
                budget_ms=10_000,
                operation_cost_ms=250,
                abandon_apps={"legacy-reports"},
            )
            operations = {
                operation["app"]: operation for operation in result["run"]["operations"]
            }
            abandoned = operations["legacy-reports"]
            self.assertEqual(abandoned["status"], "abandoned")
            self.assertTrue(abandoned["started"])
            self.assertEqual(abandoned["outcome"]["status"], "abandoned")
            self.assertEqual(abandoned["outcome"]["reason"], "policy_abandoned")
            self.assertEqual(result["run"]["status"], "abandoned")

    def test_clock_rollback_rejected_without_state_change(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            rollback(
                controller,
                "dev",
                budget_ms=10_000,
                operation_cost_ms=250,
            )
            before = canonical_json(controller.store.get_desired("dev"))
            before_clock = controller.store.get_env_clock("dev")
            with self.assertRaises(GitOpsError) as caught:
                controller.load(clock_ms=generate_samples.V1_CLOCK_MS, tolerance_ms=1)
            self.assertEqual(caught.exception.code, "E_CLOCK_ROLLBACK")
            self.assertEqual(
                canonical_json(controller.store.get_desired("dev")), before
            )
            self.assertEqual(controller.store.get_env_clock("dev"), before_clock)

    def test_verify_before_and_after_convergence(self) -> None:
        with project_fixture() as (_, project):
            controller = Controller(project)
            controller.load()
            before = controller.verify(["dev"])
            self.assertFalse(before["ok"])
            rollback(
                controller,
                "dev",
                budget_ms=10_000,
                operation_cost_ms=250,
            )
            after = controller.verify(["dev"])
            self.assertTrue(after["ok"])


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_crash_after_cluster_write_matches_uninterrupted_run(self) -> None:
        with project_fixture() as (crash_project_path, crash_project):
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "gitops_controller",
                    "--project",
                    str(crash_project_path),
                    "rollback",
                    "--env",
                    "prod",
                    "--budget-ms",
                    "10000",
                    "--crash-after",
                    "2",
                ],
                cwd=str(REPOSITORY_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(process.returncode, 75)
            self.assertEqual(process.stdout, "")
            crash_controller = Controller(crash_project)
            after_crash = crash_controller.status(["prod"])["environments"]["prod"]
            self.assertFalse(after_crash["converged"])
            self.assertEqual(after_crash["last_run"]["operation_counts"]["running"], 1)
            resumed = resume(
                crash_controller,
                "prod",
                budget_ms=10_000,
                operation_cost_ms=250,
            )
            self.assertTrue(resumed["converged"])
            crash_status = run_cli(crash_project_path, "status", "--env", "prod")[1]
            crash_commits = crash_controller.store.list_commits("prod")

        with project_fixture() as (reference_project_path, reference_project):
            reference_controller = Controller(reference_project)
            uninterrupted = rollback(
                reference_controller,
                "prod",
                budget_ms=10_000,
                operation_cost_ms=250,
            )
            reference_status = run_cli(
                reference_project_path,
                "status",
                "--env",
                "prod",
            )[1]
            reference_commits = reference_controller.store.list_commits("prod")

        self.assertEqual(
            canonical_json(resumed["run"]),
            canonical_json(uninterrupted["run"]),
        )
        self.assertEqual(canonical_json(crash_status), canonical_json(reference_status))
        self.assertEqual(canonical_json(crash_commits), canonical_json(reference_commits))

    def test_parallel_environments_and_same_environment_idempotency(self) -> None:
        with project_fixture() as (project_path, project):
            commands = [
                [
                    sys.executable,
                    "-m",
                    "gitops_controller",
                    "--project",
                    str(project_path),
                    "rollback",
                    "--env",
                    "dev",
                    "--budget-ms",
                    "10000",
                ],
                [
                    sys.executable,
                    "-m",
                    "gitops_controller",
                    "--project",
                    str(project_path),
                    "rollback",
                    "--env",
                    "prod",
                    "--budget-ms",
                    "10000",
                ],
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=str(REPOSITORY_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for command in commands
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)["converged"])
            controller = Controller(project)
            self.assertTrue(controller.verify(["dev", "prod"])["ok"])

        with project_fixture() as (project_path, project):
            command = [
                sys.executable,
                "-m",
                "gitops_controller",
                "--project",
                str(project_path),
                "rollback",
                "--env",
                "dev",
                "--budget-ms",
                "10000",
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=str(REPOSITORY_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            for process in processes:
                stdout, stderr = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertTrue(json.loads(stdout)["converged"])
            controller = Controller(project)
            self.assertTrue(controller.verify(["dev"])["ok"])
            connection = sqlite3.connect(str(project.runtime_db))
            try:
                duplicate_groups = connection.execute(
                    """
                    SELECT idempotency_key, COUNT(*) AS count
                    FROM commits
                    WHERE environment = 'dev'
                    GROUP BY idempotency_key
                    HAVING COUNT(*) > 1
                    """
                ).fetchall()
                commit_count = connection.execute(
                    "SELECT COUNT(*) FROM commits WHERE environment = 'dev'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(duplicate_groups, [])
            self.assertEqual(commit_count, 8)

    def test_two_fresh_runs_emit_identical_json(self) -> None:
        outputs = []
        for _ in range(2):
            with project_fixture() as (project_path, _):
                load_output = run_cli(project_path, "load")[1]
                plan_output = run_cli(project_path, "plan", "--env", "dev")[1]
                rollback_output = run_cli(
                    project_path,
                    "rollback",
                    "--env",
                    "dev",
                    "--budget-ms",
                    "10000",
                )[1]
                status_output = run_cli(project_path, "status", "--env", "dev")[1]
                outputs.append(
                    canonical_json(
                        {
                            "load": load_output,
                            "plan": plan_output,
                            "rollback": rollback_output,
                            "status": status_output,
                        }
                    )
                )
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
