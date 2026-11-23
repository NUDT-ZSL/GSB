from __future__ import annotations

import unittest

from sparse_recall.executor import (
    LocalWorkerPool,
    build_statistics_tasks,
    merge_worker_envelopes,
)

from tests._fixtures import load_inputs, make_dataset


class SpawnWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary, cls.data = make_dataset()
        cls.weights, cls.ingested = load_inputs(cls.data)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_spawn_pool_isolates_one_failed_batch(self) -> None:
        batch_one = self.ingested.doc_batches[1]
        batch_two = self.ingested.doc_batches[2]
        tasks = build_statistics_tasks(
            batch_one,
            weights=self.weights,
            chunk_size=8,
            compensated=True,
            decimals=12,
            injected_failure={
                "code": "E_WORKER",
                "message": "intentional batch failure",
            },
        )
        tasks.extend(
            build_statistics_tasks(
                batch_two,
                weights=self.weights,
                chunk_size=8,
                compensated=True,
                decimals=12,
            )
        )
        with LocalWorkerPool(2, use_process_pool=True) as pool:
            self.assertEqual(pool.start_method, "spawn")
            envelopes = pool.map(tasks)
        successes, failures = merge_worker_envelopes(envelopes)
        self.assertIn("doc:1", failures)
        self.assertEqual(failures["doc:1"]["code"], "E_WORKER")
        self.assertEqual(failures["doc:1"]["batch_no"], 1)
        self.assertTrue(any(item["batch_key"] == "doc:2" for item in successes))
        self.assertFalse(any(item["batch_key"] == "doc:1" for item in successes))


if __name__ == "__main__":
    unittest.main()
