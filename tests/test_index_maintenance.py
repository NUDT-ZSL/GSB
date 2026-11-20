from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "fixed_sample.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_maintenance.btree import BTree  # noqa: E402
from index_maintenance.canonical import canonical_json, sha256_json  # noqa: E402


def run_cli(
    workspace: Path,
    *arguments: str,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "index_maintenance.cli",
            "--workspace",
            str(workspace),
            *arguments,
        ],
        cwd=str(ROOT),
        env=environment,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def cli_json(
    workspace: Path,
    *arguments: str,
) -> tuple[int, dict[str, object]]:
    result = run_cli(workspace, *arguments)
    if not result.stdout.strip():
        raise AssertionError(
            f"CLI produced no JSON: rc={result.returncode} stderr={result.stderr}"
        )
    return result.returncode, json.loads(result.stdout)


def write_document(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def fresh_document() -> dict[str, object]:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def append_operation(
    document: dict[str, object],
    seq: int,
    op_type: str,
    pk: dict[str, object],
    before: dict[str, object] | None,
    after: dict[str, object] | None,
    clock_step: int,
) -> None:
    payload = {
        "seq": seq,
        "type": op_type,
        "pk": pk,
        "before": before,
        "after": after,
    }
    operation = {**payload, "content_digest": sha256_json(payload)}
    document["operations"].append(operation)
    document["config"]["clock_steps_ms"].append(clock_step)


def clean_workspace(root: Path, name: str) -> Path:
    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    rc, loaded = cli_json(workspace, "load", "--sample", str(SAMPLE))
    if rc != 0:
        raise AssertionError(f"load failed: {loaded}")
    return workspace


def apply_all(workspace: Path) -> dict[str, object]:
    rc, result = cli_json(
        workspace,
        "apply",
        "--from-seq",
        "1",
        "--to-seq",
        "15",
    )
    if rc != 0:
        raise AssertionError(f"apply failed: {result}")
    return result


def status(workspace: Path) -> dict[str, object]:
    rc, result = cli_json(workspace, "status")
    if rc != 0:
        raise AssertionError(f"status failed: {result}")
    return result["status"]


def recompute_snapshot(snapshot: dict[str, object]) -> None:
    tree = snapshot["tree"]
    for page in tree["pages"]:
        tree["page_digests"][page["page_id"]] = sha256_json(page)
    tree_core = copy.deepcopy(tree)
    tree_core.pop("digest", None)
    tree["digest"] = sha256_json(tree_core)
    snapshot_core = copy.deepcopy(snapshot)
    snapshot_core.pop("digest", None)
    snapshot["digest"] = sha256_json(snapshot_core)


class IndexMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_root = ROOT / ".test_workspaces"
        self.temp_root.mkdir(exist_ok=True)
        self.case_dir = Path(
            tempfile.mkdtemp(prefix="case-", dir=str(self.temp_root))
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.case_dir, ignore_errors=True)

    def test_deterministic_end_to_end_and_rebuild_adjudication(self) -> None:
        outputs: list[dict[str, bytes]] = []
        statuses: list[dict[str, object]] = []
        for name in ("run-a", "run-b"):
            workspace = clean_workspace(self.case_dir, name)
            apply_output = run_cli(
                workspace,
                "apply",
                "--from-seq",
                "1",
                "--to-seq",
                "15",
            )
            status_output = run_cli(workspace, "status")
            verify_output = run_cli(workspace, "verify")
            self.assertEqual(apply_output.returncode, 0, apply_output.stdout)
            self.assertEqual(status_output.returncode, 0, status_output.stdout)
            self.assertEqual(verify_output.returncode, 0, verify_output.stdout)
            outputs.append(
                {
                    "apply": apply_output.stdout.encode("utf-8"),
                    "status": status_output.stdout.encode("utf-8"),
                    "verify": verify_output.stdout.encode("utf-8"),
                }
            )
            statuses.append(json.loads(status_output.stdout)["status"])
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(
            statuses[0]["state_digest"],
            statuses[1]["state_digest"],
        )
        final = statuses[0]
        indexes = final["indexes"]
        self.assertEqual(indexes["idx_status_region"]["decision"]["mode"], "rebuild")
        self.assertEqual(
            indexes["idx_status_region"]["decision"]["first_trigger"]["condition"],
            "touched_pages",
        )
        self.assertEqual(indexes["idx_customer"]["decision"]["mode"], "incremental")
        self.assertEqual(indexes["idx_email_lookup"]["decision"]["seq"], 13)
        self.assertGreater(indexes["idx_status_region"]["split_count"], 0)
        self.assertGreater(indexes["pk"]["split_count"], 0)
        self.assertTrue(
            any(
                item["safe_reuse"] and item["index"] == "idx_email_lookup"
                for item in final["invalidations"]["items"]
                if item["seq"] == 15
            )
        )
        self.assertFalse(final["pending_attempts"])

    def test_forced_termination_and_resume(self) -> None:
        clean = clean_workspace(self.case_dir, "clean")
        apply_all(clean)
        clean_status = status(clean)

        crashed = clean_workspace(self.case_dir, "crashed")
        crash = run_cli(
            crashed,
            "apply",
            "--from-seq",
            "1",
            "--to-seq",
            "15",
            "--crash-after-split",
            "3",
        )
        self.assertEqual(crash.returncode, 97)
        self.assertEqual(crash.stdout, "")
        interrupted = status(crashed)
        self.assertLess(interrupted["ledger"]["watermark_seq"], 15)
        self.assertTrue(interrupted["pending_attempts"])
        self.assertEqual(interrupted["pending_attempts"][0]["phase"], "split")
        rc, verify_error = cli_json(crashed, "verify")
        self.assertEqual(rc, 2)
        self.assertEqual(verify_error["error"]["code"], "E_UNFINISHED_ATTEMPT")

        rc, resumed = cli_json(crashed, "resume")
        self.assertEqual(rc, 0, resumed)
        self.assertEqual(resumed["command"], "resume")
        self.assertEqual(len(resumed["discarded_attempts"]), 1)
        resumed_status = status(crashed)
        self.assertEqual(
            resumed_status["state_digest"],
            clean_status["state_digest"],
        )
        self.assertEqual(
            {
                name: (
                    value["tree_digest"],
                    value["split_count"],
                )
                for name, value in resumed_status["indexes"].items()
            },
            {
                name: (
                    value["tree_digest"],
                    value["split_count"],
                )
                for name, value in clean_status["indexes"].items()
            },
        )
        self.assertFalse(resumed_status["pending_attempts"])
        rc, verified = cli_json(crashed, "verify")
        self.assertEqual(rc, 0, verified)
        self.assertEqual(verified["state_digest"], clean_status["state_digest"])

    def test_secondary_maintenance_termination_and_resume(self) -> None:
        clean = clean_workspace(self.case_dir, "secondary-clean")
        apply_all(clean)
        clean_status = status(clean)

        crashed = clean_workspace(self.case_dir, "secondary-crashed")
        crash = run_cli(
            crashed,
            "apply",
            "--from-seq",
            "1",
            "--to-seq",
            "15",
            "--crash-after-secondary",
            "1",
        )
        self.assertEqual(crash.returncode, 97)
        interrupted = status(crashed)
        self.assertEqual(interrupted["ledger"]["watermark_seq"], 0)
        self.assertEqual(interrupted["pending_attempts"][0]["phase"], "secondary")
        rc, resumed = cli_json(crashed, "resume")
        self.assertEqual(rc, 0, resumed)
        final = status(crashed)
        self.assertEqual(final["state_digest"], clean_status["state_digest"])
        self.assertFalse(final["pending_attempts"])
        rc, verified = cli_json(crashed, "verify")
        self.assertEqual(rc, 0, verified)

    def test_concurrent_ranges_and_idempotent_duplicate_submission(self) -> None:
        clean = clean_workspace(self.case_dir, "baseline")
        apply_all(clean)
        clean_status = status(clean)

        workspace = clean_workspace(self.case_dir, "range-concurrency")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT)
        command_prefix = [
            sys.executable,
            "-m",
            "index_maintenance.cli",
            "--workspace",
            str(workspace),
            "apply",
        ]
        first = subprocess.Popen(
            command_prefix + ["--from-seq", "1", "--to-seq", "8"],
            cwd=str(ROOT),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            command_prefix + ["--from-seq", "9", "--to-seq", "15"],
            cwd=str(ROOT),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_out, first_err = first.communicate(timeout=90)
        second_out, second_err = second.communicate(timeout=90)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        self.assertEqual(
            status(workspace)["state_digest"],
            clean_status["state_digest"],
        )
        rc, verified = cli_json(workspace, "verify")
        self.assertEqual(rc, 0, verified)

        duplicate = clean_workspace(self.case_dir, "duplicate-apply")
        duplicate_prefix = [
            sys.executable,
            "-m",
            "index_maintenance.cli",
            "--workspace",
            str(duplicate),
            "apply",
        ]
        first = subprocess.Popen(
            duplicate_prefix + ["--from-seq", "1", "--to-seq", "15"],
            cwd=str(ROOT),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        second = subprocess.Popen(
            duplicate_prefix + ["--from-seq", "1", "--to-seq", "15"],
            cwd=str(ROOT),
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        first_out, first_err = first.communicate(timeout=90)
        second_out, second_err = second.communicate(timeout=90)
        self.assertEqual(first.returncode, 0, first_err)
        self.assertEqual(second.returncode, 0, second_err)
        first_json = json.loads(first_out)
        second_json = json.loads(second_out)
        self.assertEqual(
            len(first_json["applied"]) + len(second_json["applied"]),
            15,
        )
        self.assertEqual(
            status(duplicate)["state_digest"],
            clean_status["state_digest"],
        )

    def test_validation_errors_leave_no_database(self) -> None:
        def assert_error(
            name: str,
            expected_code: str,
            *,
            sample_path: str,
        ) -> None:
            workspace = self.case_dir / name
            workspace.mkdir()
            rc, result = cli_json(
                workspace,
                "load",
                "--sample",
                sample_path,
            )
            self.assertEqual(rc, 2, result)
            self.assertEqual(result["error"]["code"], expected_code)
            self.assertFalse(
                (workspace / ".index_maint" / "index.sqlite3").exists()
            )

        assert_error(
            "missing",
            "E_MANIFEST_MISSING",
            sample_path=str(self.case_dir / "does-not-exist.json"),
        )

        malformed = self.case_dir / "malformed.json"
        malformed.write_text('{"format_version": 1,', encoding="utf-8")
        assert_error(
            "malformed",
            "E_JSON_MALFORMED",
            sample_path=str(malformed),
        )

        bad_index = fresh_document()
        bad_index["indexes"][0]["columns"] = ["status", "status"]
        bad_index_path = self.case_dir / "bad-index.json"
        write_document(bad_index_path, bad_index)
        assert_error(
            "bad-index",
            "E_BAD_INDEX_DECL",
            sample_path=str(bad_index_path),
        )

        bad_schema = fresh_document()
        bad_schema["schema"]["unique_constraints"] = [["note"]]
        bad_schema_path = self.case_dir / "bad-schema.json"
        write_document(bad_schema_path, bad_schema)
        assert_error(
            "bad-schema",
            "E_BAD_SCHEMA_DECL",
            sample_path=str(bad_schema_path),
        )

        bad_type = fresh_document()
        bad_type["operations"][4]["after"]["id"] = "five"
        bad_type["operations"][4]["pk"] = {"id": "five"}
        bad_type_path = self.case_dir / "bad-type.json"
        write_document(bad_type_path, bad_type)
        assert_error(
            "bad-type",
            "E_BAD_OP",
            sample_path=str(bad_type_path),
        )

        bad_digest = fresh_document()
        bad_digest["operations"][2]["content_digest"] = "0" * 64
        bad_digest_path = self.case_dir / "bad-digest.json"
        write_document(bad_digest_path, bad_digest)
        assert_error(
            "bad-digest",
            "E_BAD_OP",
            sample_path=str(bad_digest_path),
        )

        rollback = fresh_document()
        rollback["config"]["clock_steps_ms"][-1] = -1000
        rollback_path = self.case_dir / "clock-rollback.json"
        write_document(rollback_path, rollback)
        assert_error(
            "clock-rollback",
            "E_CLOCK_ROLLBACK",
            sample_path=str(rollback_path),
        )

    def test_dynamic_errors_are_isolated_and_replayable(self) -> None:
        document = fresh_document()
        row2 = copy.deepcopy(document["operations"][1]["after"])
        duplicate_row = dict(row2)
        append_operation(
            document,
            16,
            "insert",
            {"id": 2},
            None,
            duplicate_row,
            25,
        )
        unique_row = dict(row2)
        unique_row["id"] = 99
        append_operation(
            document,
            17,
            "insert",
            {"id": 99},
            None,
            unique_row,
            26,
        )
        note_after = copy.deepcopy(document["operations"][14]["after"])
        note_after["note"] = "after-rejected-operations"
        append_operation(
            document,
            18,
            "update",
            {"id": 4},
            document["operations"][14]["after"],
            note_after,
            27,
        )
        sample_path = self.case_dir / "dynamic-errors.json"
        write_document(sample_path, document)
        workspace = self.case_dir / "dynamic"
        workspace.mkdir()
        rc, loaded = cli_json(
            workspace,
            "load",
            "--sample",
            str(sample_path),
        )
        self.assertEqual(rc, 0, loaded)
        rc, applied = cli_json(
            workspace,
            "apply",
            "--from-seq",
            "1",
            "--to-seq",
            "18",
        )
        self.assertEqual(rc, 0, applied)
        self.assertEqual(
            [item["error"]["code"] for item in applied["rejected"]],
            ["E_DUP_PK", "E_UNIQUE_VIOLATION"],
        )
        self.assertEqual(len(applied["applied"]), 16)
        final = status(workspace)
        self.assertEqual(final["ledger"]["watermark_seq"], 18)
        self.assertEqual(final["operations"]["committed"], 16)
        self.assertEqual(final["operations"]["rejected"], 2)
        rc, verified = cli_json(workspace, "verify")
        self.assertEqual(rc, 0, verified)

    def test_integrity_and_ledger_error_codes(self) -> None:
        workspace = clean_workspace(self.case_dir, "integrity")
        apply_all(workspace)
        rc, rollback = cli_json(
            workspace,
            "verify",
            "--expected-ledger-version",
            "999",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(rollback["error"]["code"], "E_LEDGER_ROLLBACK")

        digest_workspace = clean_workspace(self.case_dir, "digest")
        apply_all(digest_workspace)
        connection = sqlite3.connect(
            str(digest_workspace / ".index_maint" / "index.sqlite3")
        )
        try:
            connection.execute(
                "UPDATE snapshots SET digest=? WHERE name='idx_customer'",
                ("0" * 64,),
            )
            connection.commit()
        finally:
            connection.close()
        rc, digest_error = cli_json(digest_workspace, "verify")
        self.assertEqual(rc, 2)
        self.assertEqual(
            digest_error["error"]["code"],
            "E_INDEX_DIGEST_MISMATCH",
        )

        parent_workspace = clean_workspace(self.case_dir, "parent")
        apply_all(parent_workspace)
        connection = sqlite3.connect(
            str(parent_workspace / ".index_maint" / "index.sqlite3")
        )
        try:
            row = connection.execute(
                "SELECT snapshot_json FROM snapshots WHERE name='idx_region_order'"
            ).fetchone()
            snapshot = json.loads(row[0])
            leaf = next(
                page
                for page in snapshot["tree"]["pages"]
                if page["leaf"] and page["parent_page_id"] is not None
            )
            leaf["parent_page_id"] = "missing-parent-page"
            recompute_snapshot(snapshot)
            connection.execute(
                "UPDATE snapshots SET snapshot_json=?,digest=? WHERE name=?",
                (
                    canonical_json(snapshot),
                    snapshot["digest"],
                    "idx_region_order",
                ),
            )
            connection.commit()
        finally:
            connection.close()
        rc, parent_error = cli_json(parent_workspace, "verify")
        self.assertEqual(rc, 2)
        self.assertEqual(
            parent_error["error"]["code"],
            "E_BROKEN_PARENT_POINTER",
        )

    def test_split_height_overflow_is_rejected_and_replayable(self) -> None:
        document = fresh_document()
        document["config"]["page_capacity"] = 3
        document["config"]["max_tree_height"] = 2
        sample_path = self.case_dir / "height.json"
        write_document(sample_path, document)
        workspace = self.case_dir / "height"
        workspace.mkdir()
        rc, loaded = cli_json(
            workspace,
            "load",
            "--sample",
            str(sample_path),
        )
        self.assertEqual(rc, 0, loaded)
        rc, applied = cli_json(
            workspace,
            "apply",
            "--from-seq",
            "1",
            "--to-seq",
            "15",
        )
        self.assertEqual(rc, 0, applied)
        self.assertTrue(
            any(
                item["error"]["code"] == "E_SPLIT_HEIGHT_OVERFLOW"
                for item in applied["rejected"]
            )
        )
        rc, verified = cli_json(workspace, "verify")
        self.assertEqual(rc, 0, verified)

    def test_manual_rebuild_is_replayable(self) -> None:
        workspace = clean_workspace(self.case_dir, "manual-rebuild")
        apply_all(workspace)
        rc, rebuilt = cli_json(
            workspace,
            "rebuild",
            "--index",
            "idx_customer",
        )
        self.assertEqual(rc, 0, rebuilt)
        self.assertEqual(rebuilt["targets"][0]["index"], "idx_customer")
        final = status(workspace)
        self.assertEqual(
            final["indexes"]["idx_customer"]["decision"]["mode"],
            "manual_rebuild",
        )
        rc, verified = cli_json(workspace, "verify")
        self.assertEqual(rc, 0, verified)
        self.assertEqual(verified["ledger_version"], rebuilt["ledger_version"])

    def test_btree_splits_grouped_duplicates_and_underflow_rebalance(self) -> None:
        tree = BTree(
            "unit",
            capacity=4,
            split_factor=0.5,
            max_height=10,
            key_types=("INT",),
            pk_types=("INT",),
        )
        for key in range(1, 81):
            tree.insert_item([key], {"pk": [key], "cover": [key * 3]})
        for key in range(100, 140):
            tree.insert_item([5], {"pk": [key], "cover": [key]})
        inserted = tree.snapshot(scale=3, rounding="HALF_UP")
        self.assertGreater(tree.split_count, 0)
        self.assertTrue(tree.events)
        for event in tree.events:
            self.assertIn("parent_page_id", event)
            self.assertIn("left_sibling_page_id", event)
            self.assertIn("level", event)
        tree.integrity_check()
        group = tree.get_group([5])
        self.assertIsNotNone(group)
        self.assertEqual(len(group["items"]), 41)
        for key in range(1, 81, 2):
            tree.remove_item([key], [key])
        for key in range(100, 140, 2):
            tree.remove_item([5], [key])
        tree.integrity_check()
        self.assertEqual(tree.split_count, inserted["split_count"])
        repeated = BTree(
            "unit",
            capacity=4,
            split_factor=0.5,
            max_height=10,
            key_types=("INT",),
            pk_types=("INT",),
        )
        for key in range(1, 81):
            repeated.insert_item([key], {"pk": [key], "cover": [key * 3]})
        for key in range(100, 140):
            repeated.insert_item([5], {"pk": [key], "cover": [key]})
        self.assertEqual(
            sha256_json(inserted),
            sha256_json(repeated.snapshot(scale=3, rounding="HALF_UP")),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
