"""要求 1：确定性任务摄入与初始校验。"""

import json
import os

from statediag.errors import EValidation
from statediag.ingestion import check_revision_monotonic, ingest, parse_jsonl
from statediag.transforms import array_checksum, block_bounds, generate_state

from tests.support import SampleCase


class IngestionTest(SampleCase):
    def test_order_by_task_id_and_clock(self):
        tasks = parse_jsonl(self.tasks_path)
        keys = [(t.task_id, t.logical_clock_ms) for t in tasks]
        self.assertEqual(keys, sorted(keys))

    def test_deterministic_bounds_and_checksum(self):
        first = ingest(self.tasks_path, self.state_dim)
        second = ingest(self.tasks_path, self.state_dim)
        self.assertEqual(
            [r["block_bounds"] for r in first],
            [r["block_bounds"] for r in second],
        )
        self.assertEqual(
            [r["initial_checksum"] for r in first],
            [r["initial_checksum"] for r in second],
        )
        for record in first:
            self.assertEqual(
                record["initial_checksum"],
                array_checksum(record["initial_state"]),
            )
            self.assertEqual(
                record["block_bounds"],
                block_bounds(self.state_dim, record["task"].block_size),
            )

    def test_state_seed_deterministic(self):
        state_a = generate_state(777, self.state_dim)
        state_b = generate_state(777, self.state_dim)
        self.assertEqual(state_a.tobytes(), state_b.tobytes())

    def _write(self, rows):
        path = os.path.join(self.new_workdir(), "bad.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        return path

    def test_block_size_must_divide_dim(self):
        rows = [
            {"task_id": "x", "state_seed": 1, "num_steps": 2,
             "block_size": 5, "quant_scale": 100, "revision": 1,
             "logical_clock_ms": 1}
        ]
        with self.assertRaises(EValidation):
            ingest(self._write(rows), self.state_dim)

    def test_quant_scale_positive(self):
        rows = [
            {"task_id": "x", "state_seed": 1, "num_steps": 2,
             "block_size": 2, "quant_scale": 0, "revision": 1,
             "logical_clock_ms": 1}
        ]
        with self.assertRaises(EValidation):
            ingest(self._write(rows), self.state_dim)

    def test_revision_non_regression(self):
        tasks = parse_jsonl(self.tasks_path)
        # 构造第二个任务 revision 回退到低于首任务的序列。
        rolled = []
        for index, task in enumerate(tasks):
            revision = 0 if index == 1 else task.revision
            rolled.append(
                task.__class__(
                    task.task_id, task.state_seed, task.num_steps,
                    task.block_size, task.quant_scale, revision,
                    task.logical_clock_ms,
                )
            )
        with self.assertRaises(EValidation):
            check_revision_monotonic(rolled)
