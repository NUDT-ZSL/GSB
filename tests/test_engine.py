from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from join_engine.canonical import LogicalClock, key_token
from join_engine.engine import benchmark_query, plan_query, status_query
from join_engine.generator import build_table_stats, generate_table_rows
from join_engine.models import load_sample, parse_sample
from join_engine.partitioning import partition_for_key
from join_engine.selection import select_algorithm
from join_engine.shuffle import shuffle_side_local
from join_engine.truth import compute_truth


ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "query_sample.json"


def run_cli(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "join_engine", *args],
        cwd=ROOT,
        env=process_env,
        text=True,
        capture_output=True,
        timeout=90,
    )


def sample_dict() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def small_only_sample(join_type: str) -> dict:
    raw = sample_dict()
    raw["query"]["id"] = f"orders_users_{join_type}_small"
    raw["query"]["join_type"] = join_type
    raw["query"]["filter_selectivity"] = 0.73
    small = dict(raw["execution"]["scale_samples"][0])
    raw["execution"]["scale_samples"] = [small]
    raw["execution"]["default_scale"] = small["name"]
    return raw


class ValidationTests(unittest.TestCase):
    def test_invalid_input_has_business_code_and_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            raw = sample_dict()
            raw["tables"][0]["rows"] = 0
            sample_path = temp_path / "bad.json"
            sample_path.write_text(json.dumps(raw), encoding="utf-8")
            state_dir = temp_path / "state"
            result = run_cli(
                [
                    "run",
                    "--sample",
                    str(sample_path),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            payload = json.loads(result.stderr)
            self.assertEqual(payload["error"]["code"], "E_BAD_ROW_COUNT")
            self.assertFalse((state_dir / "state.sqlite3").exists())

    def test_plan_does_not_create_persistent_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            result = run_cli(
                [
                    "plan",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["persistent_state_written"])
            self.assertFalse(state_dir.exists())


class DeterminismTests(unittest.TestCase):
    def test_generator_is_bitwise_deterministic_and_skewed(self) -> None:
        config = load_sample(SAMPLE)
        right = config.table_map[config.query.right_table]
        first = build_table_stats(
            right,
            config.seed,
            config.execution.partitions,
            rows_override=500,
            skew_threshold=config.execution.skew_threshold,
        )
        second = build_table_stats(
            right,
            config.seed,
            config.execution.partitions,
            rows_override=500,
            skew_threshold=config.execution.skew_threshold,
        )
        self.assertEqual(
            first["generated_rows_digest"], second["generated_rows_digest"]
        )
        self.assertEqual(first["key_distribution_digest"], second["key_distribution_digest"])
        self.assertGreater(first["observed_skew_share"], 0.30)
        rows = list(generate_table_rows(right, config.seed, rows_override=500))
        self.assertEqual(len(rows), 500)
        self.assertEqual(rows, list(generate_table_rows(right, config.seed, rows_override=500)))

    def test_shuffle_is_byte_deterministic_and_partition_complete(self) -> None:
        config = load_sample(SAMPLE)
        left = config.table_map[config.query.left_table]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first_clock = LogicalClock(config.execution.logical_clock_step, 0.0)
            second_clock = LogicalClock(config.execution.logical_clock_step, 0.0)
            first = shuffle_side_local(
                table=left,
                seed=config.seed,
                rows_override=300,
                partitions=config.execution.partitions,
                directory=root / "first",
                clock=first_clock,
            )
            second = shuffle_side_local(
                table=left,
                seed=config.seed,
                rows_override=300,
                partitions=config.execution.partitions,
                directory=root / "second",
                clock=second_clock,
            )
            first_bytes = {
                path.name: path.read_bytes() for path in sorted((root / "first").glob("*.jsonl"))
            }
            second_bytes = {
                path.name: path.read_bytes() for path in sorted((root / "second").glob("*.jsonl"))
            }
            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(sum(item["rows"] for item in first), 300)
            key_owner: dict[str, int] = {}
            for commit in first:
                for line in Path(commit["relative_path"]).read_text(encoding="utf-8").splitlines():
                    row = json.loads(line)
                    key = row["values"][left.join_key]
                    token = key_token(key)
                    owner = key_owner.setdefault(token, commit["partition_id"])
                    self.assertEqual(owner, commit["partition_id"])
                    self.assertEqual(
                        partition_for_key(key, config.execution.partitions),
                        commit["partition_id"],
                    )


class SelectionTests(unittest.TestCase):
    def test_three_scales_select_three_algorithms(self) -> None:
        config = load_sample(SAMPLE)
        selected = {}
        for scale in config.execution.scale_samples:
            left = build_table_stats(
                config.table_map[config.query.left_table],
                config.seed,
                config.execution.partitions,
                rows_override=scale.left_rows,
                skew_threshold=config.execution.skew_threshold,
            )
            right = build_table_stats(
                config.table_map[config.query.right_table],
                config.seed,
                config.execution.partitions,
                rows_override=scale.right_rows,
                skew_threshold=config.execution.skew_threshold,
            )
            decision = select_algorithm(
                left,
                right,
                config.execution,
                scale_name=scale.name,
            )
            selected[scale.name] = decision["algorithm"]
            self.assertTrue(decision["first_trigger"])
            self.assertEqual(
                set(decision["rejected"]),
                {"broadcast_hash", "shuffled_hash", "sort_merge"}
                - {decision["algorithm"]},
            )
        self.assertEqual(
            selected,
            {
                "small": "broadcast_hash",
                "medium": "shuffled_hash",
                "large": "sort_merge",
            },
        )
        plan = plan_query(config, "medium")
        self.assertEqual(plan["decision"]["algorithm"], "shuffled_hash")


class JoinSemanticsTests(unittest.TestCase):
    def test_inner_left_right_match_truth_for_all_algorithms(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for join_type in ("inner", "left", "right"):
                raw = small_only_sample(join_type)
                config = parse_sample(raw)
                report = benchmark_query(
                    config,
                    state_dir=root / join_type,
                )
                self.assertTrue(report["ok"], join_type)
                scale_report = report["scales"][0]
                self.assertTrue(scale_report["algorithms"])
                for algorithm in scale_report["algorithms"]:
                    self.assertTrue(algorithm["verification"]["ok"], algorithm)
                    self.assertEqual(
                        algorithm["result_rows"], scale_report["truth"]["rows"]
                    )
                truth = compute_truth(
                    left_table=config.table_map[config.query.left_table],
                    right_table=config.table_map[config.query.right_table],
                    left_rows=scale_report["scale"]["left_rows"],
                    right_rows=scale_report["scale"]["right_rows"],
                    seed=config.seed,
                    query=config.query,
                    partitions=config.execution.partitions,
                )
                rows = truth["rows"]
                if join_type == "inner":
                    self.assertTrue(all(row["left"] is not None and row["right"] is not None for row in rows))
                elif join_type == "left":
                    self.assertTrue(any(row["right"] is None for row in rows))
                    self.assertTrue(
                        any(
                            row["right"] is None
                            and row["left"][config.query.left_key] is None
                            for row in rows
                        )
                    )
                else:
                    self.assertTrue(any(row["left"] is None for row in rows))
                    self.assertTrue(
                        any(
                            row["left"] is None
                            and row["right"][config.query.right_key] is None
                            for row in rows
                        )
                    )
                left_counts = Counter(
                    row["left_row_id"]
                    for row in rows
                    if row["left_row_id"] is not None
                )
                right_counts = Counter(
                    row["right_row_id"]
                    for row in rows
                    if row["right_row_id"] is not None
                )
                self.assertTrue(any(count > 1 for count in left_counts.values()))
                self.assertTrue(any(count > 1 for count in right_counts.values()))
                for row in rows:
                    self.assertIn("source_partitions", row)


class RecoveryTests(unittest.TestCase):
    def test_shuffle_half_write_resume_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            crashed = run_cli(
                [
                    "run",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ],
                env={"JOIN_ENGINE_CRASH_POINT": "shuffle_left_during:50"},
            )
            self.assertEqual(crashed.returncode, 137)
            status = run_cli(
                [
                    "status",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(status.returncode, 0, status.stderr)
            status_payload = json.loads(status.stdout)
            self.assertTrue(
                any(
                    item["status"] == "partial"
                    for item in status_payload["partitions"]["left"]
                )
            )
            self.assertTrue(status_payload["partial_files"]["shuffle"]["left"])
            resumed = run_cli(
                [
                    "resume",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_payload = json.loads(resumed.stdout)
            self.assertEqual(resumed_payload["status"], "completed")
            self.assertTrue(resumed_payload["metrics"]["verification"]["ok"])
            verified = run_cli(
                [
                    "verify",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)
            self.assertTrue(json.loads(verified.stdout)["ok"])

    def test_join_commits_are_skipped_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            crashed = run_cli(
                [
                    "run",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "medium",
                ],
                env={"JOIN_ENGINE_CRASH_POINT": "join_after:2"},
            )
            self.assertEqual(crashed.returncode, 137)
            resumed = run_cli(
                [
                    "resume",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "medium",
                ]
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            metrics = json.loads(resumed.stdout)["metrics"]
            self.assertEqual(metrics["join"]["partitions_skipped"], 2)
            self.assertEqual(metrics["join"]["partitions_processed"], 6)
            self.assertTrue(metrics["verification"]["ok"])
            status = json.loads(
                run_cli(
                    [
                        "status",
                        "--sample",
                        str(SAMPLE),
                        "--state-dir",
                        str(state_dir),
                        "--scale",
                        "medium",
                    ]
                ).stdout
            )
            round_statuses = {item["action"]: item["status"] for item in status["rounds"]}
            self.assertEqual(round_statuses["run"], "interrupted")
            self.assertEqual(round_statuses["resume"], "completed")


class ConcurrencyAndVersionTests(unittest.TestCase):
    def test_concurrent_same_partition_commit_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state_dir = Path(temp) / "state"
            command = [
                sys.executable,
                "-m",
                "join_engine",
                "run",
                "--sample",
                str(SAMPLE),
                "--state-dir",
                str(state_dir),
                "--scale",
                "small",
                "--partitions",
                "0",
            ]
            first = subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            second = subprocess.Popen(
                command,
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            first_out, first_err = first.communicate(timeout=90)
            second_out, second_err = second.communicate(timeout=90)
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            import sqlite3

            connection = sqlite3.connect(state_dir / "state.sqlite3")
            try:
                count = connection.execute(
                    """
                    SELECT COUNT(*) FROM partition_commits
                     WHERE side = 'left' AND partition_id = 0
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)
            state = status_query(load_sample(SAMPLE), state_dir)
            self.assertEqual(state["partitions"]["left"][0]["status"], "committed")
            final = run_cli(
                [
                    "run",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(final.returncode, 0, final.stderr)
            verified = run_cli(
                [
                    "verify",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(verified.returncode, 0, verified.stderr)

    def test_version_change_replaces_current_result_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "state"
            first = run_cli(
                [
                    "run",
                    "--sample",
                    str(SAMPLE),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_payload = json.loads(first.stdout)
            first_digest = first_payload["metrics"]["result"]["digest"]
            raw = sample_dict()
            raw["seed"] += 1
            changed_sample = root / "changed.json"
            changed_sample.write_text(json.dumps(raw), encoding="utf-8")
            second = run_cli(
                [
                    "run",
                    "--sample",
                    str(changed_sample),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_payload = json.loads(second.stdout)
            self.assertEqual(second_payload["state"]["current_version"], 2)
            self.assertNotEqual(
                second_payload["metrics"]["result"]["digest"], first_digest
            )
            status_1 = run_cli(
                [
                    "status",
                    "--sample",
                    str(changed_sample),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            status_2 = run_cli(
                [
                    "status",
                    "--sample",
                    str(changed_sample),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(status_1.stdout, status_2.stdout)
            again = run_cli(
                [
                    "run",
                    "--sample",
                    str(changed_sample),
                    "--state-dir",
                    str(state_dir),
                    "--scale",
                    "small",
                ]
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertTrue(json.loads(again.stdout)["already_completed"])


if __name__ == "__main__":
    unittest.main()
