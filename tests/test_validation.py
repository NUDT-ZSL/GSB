from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from engine_diag.errors import BusinessError
from engine_diag.models import (
    parse_plan_file,
    parse_plan_object,
    parse_platform_stats_file,
    parse_tasks_file,
)
from engine_diag.service import load_workspace
from tests.helpers import SAMPLES, load_default


class ValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_jsonl(self, name: str, rows: list[dict]) -> Path:
        path = self.dir / name
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows),
            encoding="utf-8",
        )
        return path

    def _task(self) -> dict:
        return {
            "job_id": "job-a",
            "op_kind": "matmul",
            "input_shapes": [[8, 4], [4, 6]],
            "dtype": "float32",
            "layout": "ROW_MAJOR",
            "seed": 7,
            "revision": 1,
            "logical_clock_ms": 10,
        }

    def test_samples_parse(self) -> None:
        tasks = parse_tasks_file(SAMPLES / "tasks_v1.jsonl")
        stats = parse_platform_stats_file(SAMPLES / "platform_stats.json")
        plan = parse_plan_file(SAMPLES / "plans_v1.json")
        self.assertEqual(4, len(tasks))
        self.assertEqual(52, len(stats.entries))
        self.assertEqual("p1", plan.plan_version)
        self.assertEqual(3, len(plan.candidates))

    def test_duplicate_job_id(self) -> None:
        path = self._write_jsonl("tasks.jsonl", [self._task(), self._task()])
        with self.assertRaises(BusinessError) as ctx:
            parse_tasks_file(path)
        self.assertEqual("DUPLICATE_JOB_ID", ctx.exception.code)

    def test_bad_enums_shapes_and_layouts(self) -> None:
        cases = [
            ("op_kind", "bad", "INVALID_OP_KIND"),
            ("dtype", "float64", "INVALID_DTYPE"),
            ("layout", "NCHW", "INVALID_LAYOUT"),
            ("seed", -1, "INVALID_SEED"),
            ("revision", 0, "INVALID_REVISION"),
            ("logical_clock_ms", -1, "INVALID_CLOCK"),
        ]
        for field, value, code in cases:
            with self.subTest(field=field):
                row = self._task()
                row[field] = value
                path = self._write_jsonl(f"{field}.jsonl", [row])
                with self.assertRaises(BusinessError) as ctx:
                    parse_tasks_file(path)
                self.assertEqual(code, ctx.exception.code)

        row = self._task()
        row["input_shapes"] = [[8, 4], [5, 6]]
        with self.assertRaises(BusinessError) as ctx:
            parse_tasks_file(self._write_jsonl("shape.jsonl", [row]))
        self.assertEqual("SHAPE_OP_MISMATCH", ctx.exception.code)

        row = self._task()
        row["input_shapes"] = [[8, 0], [0, 6]]
        with self.assertRaises(BusinessError) as ctx:
            parse_tasks_file(self._write_jsonl("zero.jsonl", [row]))
        self.assertEqual("INVALID_SHAPE", ctx.exception.code)

    def test_clock_regression(self) -> None:
        first = self._task()
        second = self._task()
        second["job_id"] = "job-b"
        second["logical_clock_ms"] = 9
        with self.assertRaises(BusinessError) as ctx:
            parse_tasks_file(self._write_jsonl("clock.jsonl", [first, second]))
        self.assertEqual("CLOCK_REGRESSION", ctx.exception.code)

    def test_platform_stat_ranges(self) -> None:
        entry = {
            "logical_clock_ms": 0,
            "cache_layer": "L1",
            "hit_rate": 0.9,
            "thread_occupancy": 2,
            "throughput_cost": 1.0,
        }
        cases = [
            ("cache_layer", "L9", "INVALID_CACHE_LAYER"),
            ("hit_rate", 1.1, "INVALID_HIT_RATE"),
            ("thread_occupancy", 17, "INVALID_THREAD_OCCUPANCY"),
            ("throughput_cost", -0.1, "INVALID_THROUGHPUT"),
        ]
        for field, value, code in cases:
            with self.subTest(field=field):
                item = dict(entry)
                item[field] = value
                path = self.dir / f"stats-{field}.json"
                path.write_text(
                    json.dumps({"max_threads": 16, "entries": [item]}),
                    encoding="utf-8",
                )
                with self.assertRaises(BusinessError) as ctx:
                    parse_platform_stats_file(path)
                self.assertEqual(code, ctx.exception.code)

    def test_plan_rejects_nonfinite_cost_deadline_and_cycle(self) -> None:
        plan = json.loads((SAMPLES / "plans_v1.json").read_text(encoding="utf-8"))
        bad = copy.deepcopy(plan)
        bad["jobs"]["job-alpha"]["cost_estimates"]["activation"][
            "cpu_instructions"
        ] = -1
        with self.assertRaises(BusinessError) as ctx:
            parse_plan_object(bad)
        self.assertEqual("INVALID_COST", ctx.exception.code)

        bad = copy.deepcopy(plan)
        bad["jobs"]["job-alpha"]["deadline_clock_ms"] = math.inf
        with self.assertRaises(BusinessError) as ctx:
            parse_plan_object(bad)
        self.assertEqual("INVALID_DEADLINE", ctx.exception.code)

        bad = copy.deepcopy(plan)
        bad["jobs"]["job-alpha"]["dependencies"] = ["job-beta"]
        with self.assertRaises(BusinessError) as ctx:
            parse_plan_object(bad)
        self.assertEqual("PLAN_CYCLE", ctx.exception.code)

    def test_invalid_load_does_not_create_state(self) -> None:
        bad = self._task()
        bad["dtype"] = "float64"
        tasks_path = self._write_jsonl("invalid.jsonl", [bad])
        db = self.dir / "never.sqlite3"
        with self.assertRaises(BusinessError) as ctx:
            load_workspace(db=str(db), tasks_path=str(tasks_path))
        self.assertEqual("INVALID_DTYPE", ctx.exception.code)
        self.assertFalse(db.exists())

    def test_unknown_plan_job_does_not_create_state(self) -> None:
        db = self.dir / "unknown.sqlite3"
        with self.assertRaises(BusinessError) as ctx:
            load_workspace(
                db=str(db),
                plan_path=str(SAMPLES / "plans_v1.json"),
            )
        self.assertEqual("UNKNOWN_JOB", ctx.exception.code)
        self.assertFalse(db.exists())

    def test_thread_occupancy_must_fit_plan(self) -> None:
        db = self.dir / "threads.sqlite3"
        load_default(db)
        plan = json.loads((SAMPLES / "plans_v1.json").read_text(encoding="utf-8"))
        plan["plan_version"] = "tight-threads"
        plan["max_threads"] = 2
        plan_path = self.dir / "tight.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(BusinessError) as ctx:
            load_workspace(db=str(db), plan_path=str(plan_path))
        self.assertEqual("INVALID_THREAD_OCCUPANCY", ctx.exception.code)


if __name__ == "__main__":
    unittest.main()

