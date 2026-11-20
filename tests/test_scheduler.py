from __future__ import annotations

import unittest

from anomaly_engine.scheduler import execute_tasks

from tests.helpers import make_dataset


class SchedulerTests(unittest.TestCase):
    def test_task_failure_is_isolated_and_attributed(self) -> None:
        dataset = make_dataset(n_total=40)
        payload = dataset.serializable()
        tasks = [
            {
                "task_id": "good",
                "candidate_id": "good",
                "method": "knn",
                "candidate_index": 0,
                "params": {"n_neighbors": 3, "metric": "euclidean", "seed": 1},
                "seed": 1,
                "row_chunk_size": 5,
                "batch_index": 0,
            },
            {
                "task_id": "bad",
                "candidate_id": "bad",
                "method": "unknown",
                "candidate_index": 1,
                "params": {},
                "seed": 1,
                "row_chunk_size": 5,
                "batch_index": 0,
            },
        ]
        result = execute_tasks(
            tasks=tasks,
            dataset_payload=payload,
            mode="parallel",
            workers=2,
            start_method="spawn",
            batches=[{"batch_index": 0, "task_ids": ["good", "bad"]}],
        )
        envelopes = {envelope["task_id"]: envelope for envelope in result["envelopes"]}
        self.assertEqual(envelopes["good"]["status"], "ok")
        self.assertEqual(envelopes["bad"]["status"], "error")
        error = envelopes["bad"]["error"]
        self.assertEqual(error["business_code"], "E_UNKNOWN_METHOD")
        self.assertEqual(error["stage"], "worker_dispatch")
        self.assertEqual(error["method"], "unknown")
        self.assertEqual(error["batch_index"], 0)

    def test_serial_and_parallel_valid_results_match(self) -> None:
        dataset = make_dataset(n_total=40)
        task = {
            "task_id": "knn",
            "candidate_id": "knn",
            "method": "knn",
            "candidate_index": 0,
            "params": {"n_neighbors": 4, "metric": "euclidean", "seed": 9},
            "seed": 9,
            "row_chunk_size": 7,
            "batch_index": 0,
        }
        parallel = execute_tasks(
            tasks=[task],
            dataset_payload=dataset.serializable(),
            mode="parallel",
            workers=1,
            start_method="spawn",
            batches=[{"batch_index": 0, "task_ids": ["knn"]}],
        )
        serial = execute_tasks(
            tasks=[task],
            dataset_payload=dataset.serializable(),
            mode="serial",
            workers=1,
            start_method="spawn",
            row_chunk_override=dataset.n_rows,
        )
        self.assertEqual(
            parallel["envelopes"][0]["result"]["score_checksum"],
            serial["envelopes"][0]["result"]["score_checksum"],
        )


if __name__ == "__main__":
    unittest.main()
