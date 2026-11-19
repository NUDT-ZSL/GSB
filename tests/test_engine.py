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


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def canonical_json(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [PYTHON, str(ROOT / "colagg.py"), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {args}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def run_json(*args: str, check: bool = True):
    result = run_cli(*args, check=check)
    return result.returncode, json.loads(result.stdout)


def common(temp: Path) -> list[str]:
    return [
        "--db",
        str(temp / "state.db"),
        "--output-dir",
        str(temp / "out"),
    ]


def sample_inputs() -> list[str]:
    return [
        "--batches",
        str(ROOT / "samples" / "batches.json"),
        "--query",
        str(ROOT / "samples" / "query.json"),
        "--precision",
        str(ROOT / "samples" / "precision.json"),
    ]


def make_batch(seq: int, columns: dict[str, list], digest: bool = True):
    def kind(values):
        if any(isinstance(value, float) for value in values):
            return "float"
        if any(isinstance(value, str) for value in values):
            return "string"
        return "integer"

    row_count = len(next(iter(columns.values())))
    body = {
        "seq": seq,
        "row_count": row_count,
        "columns": [
            {"name": name, "kind": kind(values), "values": values}
            for name, values in columns.items()
        ],
    }
    payload = {
        "seq": seq,
        "row_count": row_count,
        "columns": columns,
    }
    if digest:
        payload["content_digest"] = hashlib.sha256(
            canonical_json(body).encode("utf-8")
        ).hexdigest()
    return payload


class EngineIntegrationTests(unittest.TestCase):
    def test_sample_repeatability_and_full_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            self.assertEqual(
                run_json("load", *common(temp), *sample_inputs())[0], 0
            )
            self.assertEqual(
                run_json("execute", *common(temp), *sample_inputs())[0], 0
            )
            verify_code, verify = run_json(
                "verify", *common(temp), *sample_inputs()
            )
            self.assertEqual(verify_code, 0)
            self.assertTrue(verify["ok"])
            self.assertEqual(verify["mismatch_count"], 0)
            self.assertGreater(verify["checks"]["window_comparisons"], 0)
            first = (temp / "out" / "results.json").read_bytes()
            run_json("execute", *common(temp), *sample_inputs())
            second = (temp / "out" / "results.json").read_bytes()
            self.assertEqual(first, second)
            payload = json.loads(first)
            self.assertTrue(
                any(item["precision_sensitive"] for item in payload["aggregates"])
            )
            self.assertTrue(
                any(item["precision_sensitive"] for item in payload["windows"])
            )

    def test_forced_termination_resume_is_identical(self):
        with tempfile.TemporaryDirectory() as full_dir, tempfile.TemporaryDirectory() as crash_dir:
            full = Path(full_dir)
            crashed = Path(crash_dir)
            run_json("load", *common(full), *sample_inputs())
            run_json("execute", *common(full), *sample_inputs())
            expected = (full / "out" / "results.json").read_bytes()

            run_json("load", *common(crashed), *sample_inputs())
            result = run_cli(
                "execute",
                *common(crashed),
                *sample_inputs(),
                "--crash-after",
                "2",
                check=False,
            )
            self.assertEqual(result.returncode, 137)
            _, status = run_json("status", *common(crashed))
            self.assertEqual(status["batches"]["committed"], 2)
            self.assertEqual(status["batches"]["pending"], 1)
            run_json("resume", *common(crashed), *sample_inputs())
            code, verify = run_json(
                "verify", *common(crashed), *sample_inputs()
            )
            self.assertEqual(code, 0)
            self.assertTrue(verify["ok"])
            actual = (crashed / "out" / "results.json").read_bytes()
            self.assertEqual(expected, actual)

    def test_plan_incremental_reuse_and_rollback_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            args = common(temp)
            run_json("load", *args, *sample_inputs())
            run_json("execute", *args, *sample_inputs())
            code, loaded = run_json(
                "load",
                *args,
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(ROOT / "samples" / "query_v2.json"),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
            )
            self.assertEqual(code, 0)
            self.assertEqual(
                loaded["plan"]["classification"], "incremental_append"
            )
            self.assertEqual(len(loaded["reused_operators"]), 6)
            self.assertEqual(len(loaded["rebuilt_operators"]), 1)
            run_json(
                "execute",
                *args,
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(ROOT / "samples" / "query_v2.json"),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
            )
            code, verify = run_json(
                "verify",
                *args,
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(ROOT / "samples" / "query_v2.json"),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
            )
            self.assertEqual(code, 0)
            self.assertTrue(verify["ok"])
            rollback_code, rollback = run_json(
                "load", *args, *sample_inputs(), check=False
            )
            self.assertEqual(rollback_code, 2)
            self.assertEqual(rollback["error"]["code"], "E_PLAN_ROLLBACK")
            _, status = run_json("status", *args)
            self.assertEqual(status["plan"]["version"], 2)

    def test_same_version_digest_conflict_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            args = common(temp)
            query_v2 = json.loads(
                (ROOT / "samples" / "query_v2.json").read_text(encoding="utf-8")
            )
            query_v2["aggregates"].append(
                {"name": "extra_count", "op": "count"}
            )
            conflict_path = temp / "conflict.json"
            conflict_path.write_text(
                json.dumps(query_v2), encoding="utf-8"
            )
            run_json(
                "load",
                *args,
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(ROOT / "samples" / "query_v2.json"),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
            )
            code, payload = run_json(
                "load",
                *args,
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(conflict_path),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
                check=False,
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_PLAN_CONFLICT")

    def test_invalid_load_does_not_create_state(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            batches = json.loads(
                (ROOT / "samples" / "batches.json").read_text(encoding="utf-8")
            )
            batches["batches"][0]["columns"]["value"] = batches["batches"][0][
                "columns"
            ]["value"][:-1]
            batch_path = temp / "bad-batches.json"
            batch_path.write_text(json.dumps(batches), encoding="utf-8")
            code, payload = run_json(
                "load",
                *common(temp),
                "--batches",
                str(batch_path),
                "--query",
                str(ROOT / "samples" / "query.json"),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
                check=False,
            )
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["code"], "E_BAD_BATCH")
            self.assertFalse((temp / "state.db").exists())

    def test_fixed_overflow_is_isolated_per_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            batch_path = temp / "batches.json"
            batch_path.write_text(
                json.dumps(
                    {
                        "batches": [
                            make_batch(
                                0,
                                {
                                    "grp": ["A", "A"],
                                    "small": [0.1, 0.2],
                                    "big": [1_000_000_000.0, 1_000_000_000.0],
                                },
                            )
                        ]
                    }
                ),
                encoding="utf-8",
            )
            query_path = temp / "query.json"
            query_path.write_text(
                json.dumps(
                    {
                        "plan_version": 1,
                        "group_by": ["grp"],
                        "aggregates": [
                            {
                                "name": "small_sum",
                                "op": "sum",
                                "column": "small",
                            },
                            {"name": "small_count", "op": "count"},
                            {
                                "name": "big_sum",
                                "op": "sum",
                                "column": "big",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            precision_path = temp / "precision.json"
            precision_path.write_text(
                json.dumps(
                    {
                        "accumulator": "fixed",
                        "scale": 100,
                        "relative_tolerance": 1e-6,
                        "clock_step": 1,
                        "clock_start": 0,
                        "int_bits": 32,
                    }
                ),
                encoding="utf-8",
            )
            args = [
                "--batches",
                str(batch_path),
                "--query",
                str(query_path),
                "--precision",
                str(precision_path),
            ]
            run_json("load", *common(temp), *args)
            code, execution = run_json(
                "execute", *common(temp), *args, check=False
            )
            self.assertEqual(code, 1)
            self.assertEqual(execution["error"]["code"], "E_FIXED_OVERFLOW")
            states = {
                item["name"]: item for item in execution["status"]["operators"]
            }
            self.assertEqual(states["small_sum"]["status"], "ready")
            self.assertEqual(states["small_count"]["status"], "ready")
            self.assertEqual(states["big_sum"]["status"], "failed")
            self.assertEqual(states["small_sum"]["committed_batches"], 1)
            payload = json.loads(
                (temp / "out" / "results.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                {item["name"] for item in payload["aggregates"]},
                {"small_sum", "small_count"},
            )

    def test_digest_tamper_marks_batch_and_operators_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            for name in ("batches.json", "query.json", "precision.json"):
                shutil.copy(ROOT / "samples" / name, temp / name)
            args = [
                "--batches",
                str(temp / "batches.json"),
                "--query",
                str(temp / "query.json"),
                "--precision",
                str(temp / "precision.json"),
            ]
            run_json("load", *common(temp), *args)
            batches = json.loads((temp / "batches.json").read_text(encoding="utf-8"))
            batches["batches"][0]["columns"]["value"][0] = 0.1000001
            (temp / "batches.json").write_text(
                json.dumps(batches), encoding="utf-8"
            )
            code, payload = run_json(
                "execute", *common(temp), *args, check=False
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                payload["error"]["code"], "E_BATCH_DIGEST_MISMATCH"
            )
            _, status = run_json("status", *common(temp))
            self.assertEqual(status["batches"]["failed"], 1)
            self.assertTrue(
                all(item["status"] == "failed" for item in status["operators"])
            )

    def test_stale_state_returns_business_code(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            args = common(temp)
            run_json("load", *args, *sample_inputs())
            run_json("execute", *args, *sample_inputs())
            connection = sqlite3.connect(temp / "state.db")
            try:
                operator_id = connection.execute(
                    "SELECT operator_id FROM operator_state ORDER BY operator_id LIMIT 1"
                ).fetchone()[0]
                connection.execute(
                    "UPDATE operator_state SET plan_version = 0 WHERE operator_id = ?",
                    (operator_id,),
                )
                connection.commit()
            finally:
                connection.close()
            code, payload = run_json(
                "resume", *args, *sample_inputs(), check=False
            )
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"]["code"], "E_STATE_STALE")

    def test_concurrent_executors_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            args = common(temp) + sample_inputs()
            run_json("load", *args)
            processes = [
                subprocess.Popen(
                    [
                        PYTHON,
                        str(ROOT / "colagg.py"),
                        "execute",
                        *args,
                        "--max-batches",
                        "1",
                    ],
                    cwd=ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]
            outputs = [process.communicate(timeout=60) for process in processes]
            self.assertTrue(all(process.returncode == 0 for process in processes))
            for stdout, stderr in outputs:
                self.assertTrue(json.loads(stdout)["ok"], stderr)
            connection = sqlite3.connect(temp / "state.db")
            try:
                committed = connection.execute(
                    "SELECT COUNT(*) FROM batches WHERE status = 'committed'"
                ).fetchone()[0]
                duplicates = connection.execute(
                    """
                    SELECT COUNT(*) FROM (
                        SELECT operator_id, batch_seq, COUNT(*) AS n
                        FROM applied GROUP BY operator_id, batch_seq HAVING n > 1
                    )
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(committed, 2)
            self.assertEqual(duplicates, 0)
            run_json("execute", *args)
            code, verify = run_json("verify", *args)
            self.assertEqual(code, 0)
            self.assertTrue(verify["ok"])

    def test_mark_boundary_and_dual_conversion_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            query_path = temp / "query.json"
            query_path.write_text(
                json.dumps(
                    {
                        "plan_version": 1,
                        "group_by": ["grp"],
                        "aggregates": [
                            {
                                "name": "value_sum",
                                "op": "sum",
                                "column": "value",
                            }
                        ],
                        "window": {
                            "column": "value",
                            "length": 5,
                            "step": 2,
                            "boundary": "mark",
                            "functions": [{"name": "w5_sum", "op": "sum"}],
                        },
                    }
                ),
                encoding="utf-8",
            )
            args = common(temp) + [
                "--batches",
                str(ROOT / "samples" / "batches.json"),
                "--query",
                str(query_path),
                "--precision",
                str(ROOT / "samples" / "precision.json"),
            ]
            run_json("load", *args)
            run_json("execute", *args)
            code, verify = run_json("verify", *args)
            self.assertEqual(code, 0)
            self.assertTrue(verify["ok"])
            payload = json.loads(
                (temp / "out" / "results.json").read_text(encoding="utf-8")
            )
            partial = [item for item in payload["windows"] if not item["complete"]]
            self.assertTrue(partial)
            self.assertTrue(
                all(
                    item["status"] == "insufficient" and item["value"] is None
                    for item in partial
                )
            )
            connection = sqlite3.connect(temp / "state.db")
            try:
                paths = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT path FROM conversion_traces"
                    ).fetchall()
                }
                directions = {
                    row[0]
                    for row in connection.execute(
                        "SELECT DISTINCT direction FROM conversion_traces"
                    ).fetchall()
                }
            finally:
                connection.close()
            self.assertEqual(paths, {"fixed", "float64"})
            self.assertTrue(directions)


if __name__ == "__main__":
    unittest.main()
