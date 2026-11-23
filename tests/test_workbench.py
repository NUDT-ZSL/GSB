from __future__ import annotations

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
SAMPLES = ROOT / "samples"
PACKAGE_1 = SAMPLES / "packages" / "package-001.json"
PACKAGE_2 = SAMPLES / "packages" / "package-002-append.json"
PACKAGE_3 = SAMPLES / "packages" / "package-003-full.json"
EXPERIMENTS = SAMPLES / "data" / "experiments.json"
METRICS = SAMPLES / "data" / "metrics.json"
V1 = SAMPLES / "tracking" / "v1.json"
V2_MANIFEST = SAMPLES / "tracking" / "v2" / "manifest.json"
PATCH_TUNE = SAMPLES / "patches" / "patch-tune-hyperparameters.json"
PATCH_BASE = SAMPLES / "patches" / "patch-base-dataset.json"
PATCH_PARENT = SAMPLES / "patches" / "patch-eval-parent.json"
BASE = 1_761_000_000_000


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


class WorkbenchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="expwb-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "workbench.db"

    def cli(
        self,
        *args: str,
        db: Path | None = None,
        env_extra: dict[str, str] | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess:
        environment = os.environ.copy()
        environment.pop("EXPWORKBENCH_FAULT", None)
        if env_extra:
            environment.update(env_extra)
        command = [
            sys.executable,
            "-m",
            "expworkbench",
            "--db",
            str(db or self.db),
            *args,
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        if check and completed.returncode != 0:
            self.fail(
                f"command failed ({completed.returncode}): {' '.join(command)}\n"
                f"stdout={completed.stdout}\nstderr={completed.stderr}"
            )
        return completed

    def cli_json(self, *args: str, db: Path | None = None, check: bool = True):
        completed = self.cli(*args, db=db, check=check)
        if not completed.stdout.strip():
            self.fail(f"empty stdout: {completed.stderr}")
        return completed, json.loads(completed.stdout)

    def load_base(self, db: Path | None = None) -> None:
        _, result = self.cli_json(
            "load",
            "--package",
            str(PACKAGE_1),
            "--experiments",
            str(EXPERIMENTS),
            "--metrics",
            str(METRICS),
            db=db,
        )
        self.assertTrue(result["ok"])

    def refresh(self, now: int = BASE + 5000, db: Path | None = None):
        return self.cli_json(
            "refresh", "--now-ms", str(now), "--window-ms", "1000", db=db
        )[1]

    def status(self, db: Path | None = None):
        return self.cli_json("status", db=db)[1]

    def verify(self, now: int, db: Path | None = None):
        completed, result = self.cli_json(
            "verify", "--now-ms", str(now), "--window-ms", "1000", db=db, check=False
        )
        return completed, result

    def metric_map(self, status: dict):
        return {
            (experiment["experiment_id"], metric["metric"]): metric
            for experiment in status["experiments"]
            for metric in experiment["metrics"]
        }

    # ------------------------------------------------------------------ load
    def test_merge_preserves_source_conversion_and_decision(self) -> None:
        self.load_base()
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        try:
            view = {
                (row["experiment_id"], row["metric"]): row
                for row in connection.execute(
                    "SELECT * FROM snapshot_view_entries WHERE snapshot_version='pkg-001'"
                )
            }
            base_loss = view[("exp-base", "loss")]
            self.assertEqual(base_loss["selected_source_format"], "v2")
            self.assertEqual(base_loss["selected_generation"], 2)
            decision = json.loads(base_loss["decision_json"])
            self.assertEqual(decision["reason"], "higher_logical_clock")
            self.assertTrue(
                any(
                    rejected["source_format"] == "v1"
                    for rejected in decision["rejected_candidates"]
                )
            )
            base_accuracy = view[("exp-base", "accuracy")]
            self.assertEqual(base_accuracy["selected_source_format"], "v1")
            self.assertEqual(base_accuracy["conversion_scale"], 1000)
            self.assertEqual(base_accuracy["conversion_remainder_ms"], 0)
            self.assertEqual(base_accuracy["rounding_direction"], "floor")
            self.assertEqual(
                base_accuracy["absolute_refresh_at_ms"], BASE + 100 + 2 * 1000
            )
            source_count = connection.execute(
                "SELECT COUNT(*) FROM snapshot_source_entries WHERE snapshot_version='pkg-001'"
            ).fetchone()[0]
            self.assertEqual(source_count, 9)
        finally:
            connection.close()

    def test_invalid_inputs_have_codes_and_no_persistence(self) -> None:
        experiments = load_json(EXPERIMENTS)
        metrics = load_json(METRICS)
        v1 = load_json(V1)

        cases = []

        duplicate = json.loads(json.dumps(experiments))
        duplicate["experiments"].append(json.loads(json.dumps(duplicate["experiments"][0])))
        cases.append(("duplicate_experiment", duplicate, metrics, None, "E_DUP_EXPERIMENT_ID"))

        cycle = json.loads(json.dumps(experiments))
        for experiment in cycle["experiments"]:
            experiment.pop("fingerprint", None)
        cycle["experiments"][0]["parent_id"] = "exp-eval"
        cases.append(("lineage_cycle", cycle, metrics, None, "E_LINEAGE_CYCLE"))

        bad_hyper = json.loads(json.dumps(experiments))
        bad_hyper["experiments"][0]["hyperparameters"]["nested"] = [1, 2]
        cases.append(("bad_hyperparameter", bad_hyper, metrics, None, "E_HYPERPARAM_TYPE"))

        bad_reference = json.loads(json.dumps(experiments))
        for experiment in bad_reference["experiments"]:
            experiment.pop("fingerprint", None)
        bad_reference["experiments"][2]["metric_references"]["loss_delta"]["metric"] = "missing"
        cases.append(
            ("bad_metric_reference", bad_reference, metrics, "__v2_only__", "E_METRIC_REFERENCE")
        )

        bad_step = json.loads(json.dumps(metrics))
        tune_loss = [
            record
            for record in bad_step["records"]
            if record["experiment_id"] == "exp-tune" and record["metric"] == "loss"
        ]
        tune_loss[1]["step"] = tune_loss[0]["step"]
        tune_loss[1].pop("record_digest", None)
        cases.append(("bad_step", experiments, bad_step, None, "E_METRIC_STEP"))

        duplicate_record_id = json.loads(json.dumps(metrics))
        duplicate_record_id["records"][1]["record_id"] = duplicate_record_id["records"][0][
            "record_id"
        ]
        cases.append(
            ("duplicate_record_id", experiments, duplicate_record_id, None, "E_METRIC_ID_DUP")
        )

        bad_value = json.loads(json.dumps(metrics))
        bad_value["records"][0]["value"] = "not-a-number"
        bad_value["records"][0].pop("record_digest", None)
        cases.append(("bad_value", experiments, bad_value, None, "E_METRIC_VALUE"))

        duplicate_alias = json.loads(json.dumps(v1))
        duplicate_alias["entries"][1]["alias"] = duplicate_alias["entries"][0]["alias"]
        cases.append(("duplicate_v1_alias", experiments, metrics, duplicate_alias, "E_V1_ALIAS_DUP"))

        bad_fingerprint = json.loads(json.dumps(v1))
        bad_fingerprint["entries"][0]["fingerprint"] = "sha256:" + "0" * 64
        cases.append(
            ("bad_v1_fingerprint", experiments, metrics, bad_fingerprint, "E_V1_FINGERPRINT")
        )

        negative_refresh = json.loads(json.dumps(v1))
        negative_refresh["entries"][0]["relative_refresh_seconds"] = -1
        cases.append(
            ("bad_v1_refresh", experiments, metrics, negative_refresh, "E_V1_REFRESH")
        )

        for name, experiment_doc, metric_doc, v1_doc, expected_code in cases:
            with self.subTest(name=name):
                case_dir = self.root / name
                case_dir.mkdir()
                experiment_path = case_dir / "experiments.json"
                metric_path = case_dir / "metrics.json"
                write_json(experiment_path, experiment_doc)
                write_json(metric_path, metric_doc)
                db = case_dir / "state.db"
                if v1_doc is None:
                    command = [
                        "load",
                        "--package",
                        str(PACKAGE_1),
                        "--experiments",
                        str(experiment_path),
                        "--metrics",
                        str(metric_path),
                    ]
                elif v1_doc == "__v2_only__":
                    command = [
                        "load",
                        "--v2-manifest",
                        str(V2_MANIFEST),
                        "--experiments",
                        str(experiment_path),
                        "--metrics",
                        str(metric_path),
                    ]
                else:
                    v1_path = case_dir / "v1.json"
                    write_json(v1_path, v1_doc)
                    command = [
                        "load",
                        "--v1",
                        str(v1_path),
                        "--v2-manifest",
                        str(V2_MANIFEST),
                        "--experiments",
                        str(experiment_path),
                        "--metrics",
                        str(metric_path),
                    ]
                completed = self.cli(*command, db=db)
                self.assertNotEqual(completed.returncode, 0)
                result = json.loads(completed.stdout)
                self.assertEqual(result["error"]["code"], expected_code)
                self.assertFalse(db.exists(), "invalid input must not create state")

    def test_v2_hash_and_generation_validation(self) -> None:
        case_dir = self.root / "v2-invalid"
        shutil.copytree(SAMPLES / "tracking", case_dir / "tracking")
        manifest_path = case_dir / "tracking" / "v2" / "manifest.json"

        bad_hash = load_json(manifest_path)
        bad_hash["shards"][0]["sha256"] = "sha256:" + "1" * 64
        write_json(manifest_path, bad_hash)
        metadata_copy = case_dir / "tracking" / "v2" / "metadata.sqlite3"
        connection = sqlite3.connect(metadata_copy)
        try:
            connection.execute(
                "UPDATE tracking_shards SET sha256 = ?", (bad_hash["shards"][0]["sha256"],)
            )
            connection.commit()
        finally:
            connection.close()
        db = case_dir / "hash.db"
        completed = self.cli(
            "load",
            "--v1",
            str(case_dir / "tracking" / "v1.json"),
            "--v2-manifest",
            str(manifest_path),
            "--experiments",
            str(EXPERIMENTS),
            "--metrics",
            str(METRICS),
            db=db,
        )
        self.assertEqual(json.loads(completed.stdout)["error"]["code"], "E_V2_HASH")
        self.assertFalse(db.exists())

        metadata = case_dir / "empty-meta.sqlite3"
        sqlite3.connect(metadata).close()
        shard = case_dir / "tracking" / "v2" / "shards" / "shard-000.json"
        import hashlib

        shard_hash = "sha256:" + hashlib.sha256(shard.read_bytes()).hexdigest()
        bad_generation = {
            "format": "exp-trace-v2-manifest",
            "snapshot_version": "bad-generation",
            "version_number": 1,
            "tracking_generation": 1,
            "tracking_generation_floor": 2,
            "logical_clock_ms": BASE + 1000,
            "metadata_db": "empty-meta.sqlite3",
            "shards": [
                {
                    "path": "tracking/v2/shards/shard-000.json",
                    "sha256": shard_hash,
                    "trace_generation": 1,
                }
            ],
        }
        generation_path = case_dir / "bad-generation.json"
        write_json(generation_path, bad_generation)
        db = case_dir / "generation.db"
        completed = self.cli(
            "load",
            "--v2-manifest",
            str(generation_path),
            "--experiments",
            str(EXPERIMENTS),
            "--metrics",
            str(METRICS),
            db=db,
        )
        self.assertEqual(json.loads(completed.stdout)["error"]["code"], "E_V2_GENERATION")
        self.assertFalse(db.exists())

    # --------------------------------------------------------------- refresh
    def test_refresh_is_deterministic_and_idempotent(self) -> None:
        self.load_base()
        first = self.refresh()
        second = self.refresh()
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["windows_updated"], 25)
        connection = sqlite3.connect(self.db)
        try:
            record_count = connection.execute(
                "SELECT COUNT(*) FROM metric_records"
            ).fetchone()[0]
            assignment_count = connection.execute(
                "SELECT COUNT(*) FROM window_record_assignments"
            ).fetchone()[0]
            window_count = connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(record_count, 25)
        self.assertEqual(assignment_count, 25)
        self.assertEqual(window_count, 25)
        completed, verified = self.verify(BASE + 5000)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(verified["consistent"])

        second_db = self.root / "second.db"
        self.load_base(second_db)
        self.refresh(db=second_db)
        self.assertEqual(self.status(), self.status(second_db))

    def test_window_boundary_and_open_window(self) -> None:
        self.load_base()
        result = self.refresh(BASE + 4501)
        # Only windows ending at or before the injected clock are committed.
        self.assertLess(result["windows_updated"], 25)
        status = self.status()
        for experiment in status["experiments"]:
            for metric in experiment["metrics"]:
                for window in metric["windows"]:
                    self.assertLessEqual(window["window_end_ms"], BASE + 4501)
                    self.assertTrue(
                        window["window_start_ms"] <= BASE + 4501 < window["window_end_ms"]
                        or window["window_end_ms"] <= BASE + 4501
                    )

    # ---------------------------------------------------------------- patches
    def test_patch_closure_reuses_unaffected_bytes(self) -> None:
        self.load_base()
        self.refresh()
        before = self.status()
        _, patch = self.cli_json("patch", "--file", str(PATCH_TUNE))
        after = self.status()
        self.assertEqual(
            {(item["experiment_id"], item["metric"]) for item in patch["invalidated"]},
            {
                ("exp-tune", "loss"),
                ("exp-tune", "accuracy"),
                ("exp-eval", "loss_delta"),
            },
        )
        before_metrics = self.metric_map(before)
        after_metrics = self.metric_map(after)
        for key in [
            ("exp-base", "loss"),
            ("exp-base", "accuracy"),
        ]:
            self.assertEqual(before_metrics[key], after_metrics[key])
        for key in [
            ("exp-tune", "loss"),
            ("exp-tune", "accuracy"),
            ("exp-eval", "loss_delta"),
        ]:
            self.assertNotEqual(
                before_metrics[key]["summary"]["summary_digest"],
                after_metrics[key]["summary"]["summary_digest"],
            )
        self.assertTrue(
            all(
                item["rejected_reuse_candidates"]
                for item in patch["invalidated"]
                if item["metric"] != "legacy_loss"
            )
        )
        self.assertEqual(after["pending_invalidation_events"], 0)
        completed, verified = self.verify(BASE + 6000)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(verified["consistent"])

        duplicate_completed, duplicate = self.cli_json(
            "patch", "--file", str(PATCH_TUNE)
        )
        self.assertEqual(duplicate_completed.returncode, 0)
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["data_epoch"], patch["data_epoch"])

    def test_dataset_and_parent_patch_closures(self) -> None:
        self.load_base()
        self.refresh()
        _, dataset_patch = self.cli_json("patch", "--file", str(PATCH_BASE))
        self.assertEqual(
            {(item["experiment_id"], item["metric"]) for item in dataset_patch["invalidated"]},
            {
                ("exp-base", "loss"),
                ("exp-base", "accuracy"),
                ("exp-tune", "loss"),
                ("exp-tune", "accuracy"),
                ("exp-eval", "loss_delta"),
            },
        )
        self.assertEqual(dataset_patch["reused"], [])
        completed, verified = self.verify(BASE + 7000)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(verified["consistent"])

    def test_non_descendant_metric_reference_enters_closure(self) -> None:
        from expworkbench.patches import compute_closure
        from expworkbench.validation import validate_experiments

        documents = {
            "experiments": [
                {
                    "id": "base",
                    "parent_id": None,
                    "hyperparameters": {"lr": 1},
                    "dataset_version": "d1",
                    "code_commit": "c1",
                    "baseline_metrics": {"loss": 1},
                    "logical_clock_ms": 1,
                    "metric_references": {},
                },
                {
                    "id": "child",
                    "parent_id": "base",
                    "hyperparameters": {"lr": 1},
                    "dataset_version": "d1",
                    "code_commit": "c2",
                    "baseline_metrics": {"loss": 1},
                    "logical_clock_ms": 2,
                    "metric_references": {},
                },
                {
                    "id": "auditor",
                    "parent_id": None,
                    "hyperparameters": {},
                    "dataset_version": "d1",
                    "code_commit": "c3",
                    "baseline_metrics": {"quality": 1},
                    "logical_clock_ms": 3,
                    "metric_references": {
                        "quality": {"experiment_id": "base", "metric": "loss"}
                    },
                },
                {
                    "id": "auditor-child",
                    "parent_id": "auditor",
                    "hyperparameters": {},
                    "dataset_version": "d1",
                    "code_commit": "c4",
                    "baseline_metrics": {"score": 1},
                    "logical_clock_ms": 4,
                    "metric_references": {},
                },
                {
                    "id": "unrelated",
                    "parent_id": None,
                    "hyperparameters": {},
                    "dataset_version": "d1",
                    "code_commit": "c5",
                    "baseline_metrics": {"solo": 1},
                    "logical_clock_ms": 5,
                    "metric_references": {},
                },
            ]
        }
        experiments = {
            experiment.id: experiment
            for experiment in validate_experiments(documents)
        }
        metrics = {
            ("base", "loss"): None,
            ("child", "loss"): None,
            ("auditor", "quality"): None,
            ("auditor-child", "score"): None,
            ("unrelated", "solo"): None,
        }
        closure = compute_closure(
            experiments,
            experiments,
            ["base"],
            {"base": ["hyperparameters"]},
            metrics,
        )
        self.assertEqual(
            set(closure.affected),
            {
                ("base", "loss"),
                ("child", "loss"),
                ("auditor", "quality"),
                ("auditor-child", "score"),
            },
        )
        self.assertEqual(
            closure.affected[("auditor", "quality")]["reason"],
            "metric_reference_invalidation",
        )
        self.assertEqual(
            {(item["experiment_id"], item["metric"]) for item in closure.reused},
            {("unrelated", "solo")},
        )

        parent_db = self.root / "parent.db"
        self.load_base(parent_db)
        self.refresh(db=parent_db)
        _, parent_patch = self.cli_json(
            "patch", "--file", str(PATCH_PARENT), db=parent_db
        )
        self.assertEqual(
            {(item["experiment_id"], item["metric"]) for item in parent_patch["invalidated"]},
            {("exp-eval", "loss_delta")},
        )
        completed, verified = self.verify(BASE + 8000, db=parent_db)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(verified["consistent"])

    # --------------------------------------------------------------- snapshot
    def test_snapshot_noop_append_replace_and_rollback(self) -> None:
        self.load_base()
        _, noop = self.cli_json("load", "--package", str(PACKAGE_1))
        self.assertEqual(noop["classification"], "noop")
        self.refresh()
        self.cli_json("patch", "--file", str(PATCH_TUNE))
        _, appended = self.cli_json("load", "--package", str(PACKAGE_2))
        self.assertEqual(appended["classification"], "incremental_append")
        self.assertEqual(appended["snapshot_version"], "pkg-002")
        _, replaced = self.cli_json("load", "--package", str(PACKAGE_3))
        self.assertEqual(replaced["classification"], "full_replace")
        self.assertEqual(replaced["snapshot_version"], "pkg-003")
        completed = self.cli("load", "--package", str(PACKAGE_2))
        self.assertNotEqual(completed.returncode, 0)
        error = json.loads(completed.stdout)["error"]
        self.assertEqual(error["code"], "E_SNAPSHOT_ROLLBACK")
        self.assertEqual(self.status()["snapshot_version"], "pkg-003")
        self.refresh(BASE + 9000)
        verify_completed, verified = self.verify(BASE + 9000)
        self.assertEqual(verify_completed.returncode, 0)
        self.assertTrue(verified["consistent"])

    # --------------------------------------------------------------- recovery
    def _prepare_three_dbs(self):
        uninterrupted = self.root / "uninterrupted.db"
        recovered = self.root / "recovered.db"
        for db in (uninterrupted, recovered):
            self.load_base(db)
            self.refresh(db=db)
        return uninterrupted, recovered

    def test_crash_after_patch_commit_resumes_identically(self) -> None:
        uninterrupted, recovered = self._prepare_three_dbs()
        self.cli_json("patch", "--file", str(PATCH_TUNE), db=uninterrupted)
        crashed = self.cli(
            "patch",
            "--file",
            str(PATCH_TUNE),
            db=recovered,
            env_extra={"EXPWORKBENCH_FAULT": "after_patch_commit"},
        )
        self.assertEqual(crashed.returncode, 86)
        pending_status = self.status(recovered)
        self.assertEqual(pending_status["pending_invalidation_events"], 1)
        self.assertEqual(pending_status["invalidations"][0]["status"], "pending")
        resumed = self.cli_json("resume", db=recovered)[1]
        self.assertEqual(resumed["remaining_pending_events"], 0)
        self.assertEqual(self.status(uninterrupted), self.status(recovered))
        completed, verified = self.verify(BASE + 6000, db=recovered)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(verified["consistent"])

    def test_crash_before_invalidation_commit_rolls_back_and_replays(self) -> None:
        uninterrupted, recovered = self._prepare_three_dbs()
        self.cli_json("patch", "--file", str(PATCH_TUNE), db=uninterrupted)
        crashed = self.cli(
            "patch",
            "--file",
            str(PATCH_TUNE),
            db=recovered,
            env_extra={"EXPWORKBENCH_FAULT": "before_invalidation_commit"},
        )
        self.assertEqual(crashed.returncode, 86)
        connection = sqlite3.connect(recovered)
        try:
            epoch = connection.execute(
                "SELECT value FROM meta WHERE key='data_epoch'"
            ).fetchone()[0]
            pending = connection.execute(
                "SELECT COUNT(*) FROM invalidation_events WHERE status!='complete'"
            ).fetchone()[0]
            complete_summaries = connection.execute(
                "SELECT COUNT(*) FROM metric_summaries WHERE last_event_id IS NOT NULL"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(epoch, "1")
        self.assertEqual(pending, 1)
        self.assertEqual(complete_summaries, 0)
        self.cli_json("resume", db=recovered)
        self.assertEqual(self.status(uninterrupted), self.status(recovered))

    def test_crash_during_snapshot_switch_keeps_old_pointer(self) -> None:
        uninterrupted = self.root / "uninterrupted.db"
        recovered = self.root / "recovered.db"
        for db in (uninterrupted, recovered):
            self.load_base(db)
            self.refresh(db=db)
        self.cli_json("load", "--package", str(PACKAGE_2), db=uninterrupted)
        self.refresh(BASE + 9000, db=uninterrupted)
        crashed = self.cli(
            "load",
            "--package",
            str(PACKAGE_2),
            db=recovered,
            env_extra={"EXPWORKBENCH_FAULT": "before_snapshot_pointer_switch"},
        )
        self.assertEqual(crashed.returncode, 86)
        old_status = self.status(recovered)
        self.assertEqual(old_status["snapshot_version"], "pkg-001")
        self.assertEqual(
            sum(
                len(metric["windows"])
                for experiment in old_status["experiments"]
                for metric in experiment["metrics"]
            ),
            25,
        )
        _, retried = self.cli_json("load", "--package", str(PACKAGE_2), db=recovered)
        self.assertEqual(retried["classification"], "incremental_append")
        self.refresh(BASE + 9000, db=recovered)
        self.assertEqual(self.status(uninterrupted), self.status(recovered))

    def test_refresh_crash_after_commit_is_idempotent(self) -> None:
        self.load_base()
        crashed = self.cli(
            "refresh",
            "--now-ms",
            str(BASE + 5000),
            env_extra={"EXPWORKBENCH_FAULT": "after_refresh_commit"},
        )
        self.assertEqual(crashed.returncode, 86)
        replay = self.refresh()
        self.assertTrue(replay["duplicate"])
        connection = sqlite3.connect(self.db)
        try:
            windows = connection.execute("SELECT COUNT(*) FROM windows").fetchone()[0]
            assignments = connection.execute(
                "SELECT COUNT(*) FROM window_record_assignments"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(windows, 25)
        self.assertEqual(assignments, 25)
        _, verified = self.verify(BASE + 5000)
        self.assertTrue(verified["consistent"])

    # ------------------------------------------------------------- concurrency
    def test_concurrent_refresh_and_patch_are_single_commit(self) -> None:
        self.load_base()
        command = [
            sys.executable,
            "-m",
            "expworkbench",
            "--db",
            str(self.db),
            "refresh",
            "--now-ms",
            str(BASE + 5000),
        ]
        first = subprocess.Popen(
            command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        second = subprocess.Popen(
            command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        outputs = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        self.assertEqual(sum(1 for output in outputs if output["duplicate"]), 1)

        patch_command = [
            sys.executable,
            "-m",
            "expworkbench",
            "--db",
            str(self.db),
            "patch",
            "--file",
            str(PATCH_TUNE),
        ]
        processes = [
            subprocess.Popen(
                patch_command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        patch_outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            patch_outputs.append(json.loads(stdout))
        self.assertEqual(sum(1 for output in patch_outputs if output["duplicate"]), 1)
        self.assertEqual({output["data_epoch"] for output in patch_outputs}, {1})

        load_command = [
            sys.executable,
            "-m",
            "expworkbench",
            "--db",
            str(self.db),
            "load",
            "--package",
            str(PACKAGE_2),
        ]
        load_processes = [
            subprocess.Popen(
                load_command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        load_outputs = []
        for process in load_processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr)
            load_outputs.append(json.loads(stdout))
        self.assertEqual(
            sorted(output["classification"] for output in load_outputs),
            ["incremental_append", "noop"],
        )
        self.assertEqual({output["snapshot_version"] for output in load_outputs}, {"pkg-002"})
        self.refresh(BASE + 9000)
        _, verified = self.verify(BASE + 9000)
        self.assertTrue(verified["consistent"])

    # --------------------------------------------------------------- diagnosis
    def test_verify_detects_first_divergence_with_values(self) -> None:
        self.load_base()
        self.refresh()
        connection = sqlite3.connect(self.db)
        try:
            connection.execute(
                """
                UPDATE windows SET mean_text='999'
                WHERE rowid = (
                    SELECT rowid FROM windows
                    WHERE snapshot_version='pkg-001' AND experiment_id='exp-base'
                      AND metric='loss'
                    ORDER BY window_start_ms LIMIT 1
                )
                """
            )
            connection.commit()
        finally:
            connection.close()
        completed, report = self.verify(BASE + 5000)
        self.assertEqual(completed.returncode, 1)
        self.assertFalse(report["consistent"])
        first = report["first_divergence"]
        self.assertEqual(first["experiment_id"], "exp-base")
        self.assertEqual(first["metric"], "loss")
        self.assertEqual(first["window_number"], BASE // 1000)
        self.assertEqual(first["incremental_value"]["mean"], "999")
        self.assertNotEqual(first["incremental_value"]["mean"], first["truth_value"]["mean"])
        self.assertEqual(first["depends_on_snapshot_version"], "pkg-001")
        self.assertIn("mean_text", first["mismatched_fields"])

    def test_status_is_stable_and_web_api_works(self) -> None:
        self.load_base()
        self.refresh()
        first = self.cli("status").stdout
        second = self.cli("status").stdout
        self.assertEqual(first, second)
        from expworkbench.web import create_app

        app = create_app(str(self.db))
        client = app.test_client()
        page = client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Experiment Trace Workbench", page.data)
        status = client.get("/api/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["snapshot_version"], "pkg-001")
        diagnosis = client.get("/api/diagnose")
        self.assertEqual(diagnosis.status_code, 200)
        self.assertTrue(diagnosis.get_json()["consistent"])
        lineage = client.get("/api/lineage").get_json()
        self.assertEqual(len(lineage["nodes"]), 3)
        self.assertEqual(len(lineage["edges"]), 2)


if __name__ == "__main__":
    unittest.main()

