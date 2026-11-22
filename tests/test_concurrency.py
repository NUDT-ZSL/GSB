from __future__ import annotations

import threading
import time
import unittest

from sparse_inc.codes import E_RESOURCE_LIMIT, E_WRITE_BUSY
from sparse_inc.config import ComponentConfig
from sparse_inc.errors import BusinessError
from sparse_inc.service import SparseIncrementalService

from tests.support import make_sample_directory


class ConcurrencyAndResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()
        cls.case = cls.cases["spmm_inc_demo"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_second_writer_gets_busy_without_blocking(self) -> None:
        service = SparseIncrementalService(self.case)
        entered = threading.Event()
        release = threading.Event()

        def hold_writer() -> None:
            with service._mutex:
                entered.set()
                release.wait(timeout=3)

        holder = threading.Thread(target=hold_writer)
        holder.start()
        self.assertTrue(entered.wait(timeout=2))
        started = time.monotonic()
        with self.assertRaises(BusinessError) as caught:
            service.submit_batch(self.case.write_batches[0], batch_index=0)
        elapsed = time.monotonic() - started
        release.set()
        holder.join(timeout=2)
        self.assertEqual(caught.exception.business_code, E_WRITE_BUSY)
        self.assertLess(elapsed, 0.5)
        self.assertEqual(service.ledger.version, 0)

    def test_reader_waits_for_atomic_state_swap(self) -> None:
        service = SparseIncrementalService(self.case)
        commit_entered = threading.Event()
        allow_commit = threading.Event()
        original = service._commit_locked

        def delayed_commit(batch, batch_index):  # type: ignore[no-untyped-def]
            commit_entered.set()
            allow_commit.wait(timeout=3)
            return original(batch, batch_index)

        service._commit_locked = delayed_commit  # type: ignore[method-assign]
        writer = threading.Thread(
            target=service.submit_batch,
            args=(self.case.write_batches[0],),
            kwargs={"batch_index": 0},
        )
        writer.start()
        self.assertTrue(commit_entered.wait(timeout=2))
        query_done = threading.Event()
        observed: dict[str, int] = {}

        def query() -> None:
            observed["version"] = service.ledger.version
            query_done.set()

        reader = threading.Thread(target=query)
        reader.start()
        self.assertFalse(query_done.wait(timeout=0.15))
        allow_commit.set()
        writer.join(timeout=3)
        reader.join(timeout=3)
        self.assertTrue(query_done.is_set())
        self.assertEqual(observed["version"], 1)
        self.assertEqual(len(service.snapshot_view().blocks), 5)

    def test_resident_batch_limit_returns_resource_limit(self) -> None:
        service = SparseIncrementalService(
            self.case,
            ComponentConfig(max_resident_batches=1),
        )
        entered = threading.Event()
        release = threading.Event()
        original = service._commit_locked

        def delayed_commit(batch, batch_index):  # type: ignore[no-untyped-def]
            entered.set()
            release.wait(timeout=3)
            return original(batch, batch_index)

        service._commit_locked = delayed_commit  # type: ignore[method-assign]
        first = threading.Thread(
            target=service.submit_batch,
            args=(self.case.write_batches[0],),
            kwargs={"batch_index": 0},
        )
        first.start()
        self.assertTrue(entered.wait(timeout=2))
        with self.assertRaises(BusinessError) as caught:
            service.submit_batch(self.case.write_batches[1], batch_index=1)
        release.set()
        first.join(timeout=3)
        self.assertEqual(caught.exception.business_code, E_RESOURCE_LIMIT)
        self.assertEqual(caught.exception.segment, "resident_batches")
        self.assertEqual(service.ledger.version, 1)

    def test_strict_row_buffer_limit_does_not_apply_batch(self) -> None:
        service = SparseIncrementalService(
            self.case,
            ComponentConfig(
                row_block_size=2,
                max_row_buffers=1,
                resource_policy="strict",
            ),
        )
        before = service.snapshot_view()
        with self.assertRaises(BusinessError) as caught:
            service.submit_batch(self.case.write_batches[0], batch_index=0)
        after = service.snapshot_view()
        self.assertEqual(caught.exception.business_code, E_RESOURCE_LIMIT)
        self.assertEqual(caught.exception.segment, "row_buffers")
        self.assertEqual(before.ledger_snapshot, after.ledger_snapshot)
        self.assertEqual(before.result_snapshot, after.result_snapshot)

    def test_degrade_policy_marks_row_buffer_and_parallel_caps(self) -> None:
        row_service = SparseIncrementalService(
            self.case,
            ComponentConfig(
                row_block_size=2,
                max_row_buffers=1,
                max_parallel_intervals=1,
                resource_policy="degrade",
            ),
        )
        row_result = row_service.submit_batch(self.case.write_batches[0], batch_index=0)
        self.assertTrue(row_result["degraded"])
        self.assertIn(
            "row_buffer_limit_chunked",
            row_result["degradation_categories"],
        )

        parallel_service = SparseIncrementalService(
            self.case,
            ComponentConfig(
                row_block_size=2,
                max_row_buffers=4,
                max_parallel_intervals=1,
                resource_policy="degrade",
            ),
        )
        parallel_result = parallel_service.submit_batch(
            self.case.write_batches[0],
            batch_index=0,
        )
        self.assertTrue(parallel_result["degraded"])
        self.assertIn(
            "parallel_interval_limit_capped",
            parallel_result["degradation_categories"],
        )


if __name__ == "__main__":
    unittest.main()
