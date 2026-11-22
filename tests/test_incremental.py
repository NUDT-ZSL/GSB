from __future__ import annotations

import dataclasses
import unittest

import numpy as np

from sparse_inc.codes import E_INCONSISTENT, OK
from sparse_inc.config import ComponentConfig
from sparse_inc.errors import BusinessError
from sparse_inc.service import SparseIncrementalService

from tests.support import make_sample_directory


class IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def _run_case(self, case_id: str) -> SparseIncrementalService:
        case = self.cases[case_id]
        service = SparseIncrementalService(case, ComponentConfig())
        for index, batch in enumerate(case.write_batches):
            service.submit_batch(batch, batch_index=index)
        return service

    def test_each_event_partitions_all_rows(self) -> None:
        case = self.cases["spmm_inc_demo"]
        service = SparseIncrementalService(case)
        for index, batch in enumerate(case.write_batches):
            service.submit_batch(batch, batch_index=index)
            event = service.events[-1]
            intervals = [
                (item["row_start"], item["row_end"])
                for item in event.recompute_intervals + event.reuse_intervals
            ]
            intervals.sort()
            cursor = 0
            for start, end in intervals:
                self.assertEqual(start, cursor)
                self.assertLess(start, end)
                cursor = end
            self.assertEqual(cursor, case.m)
            self.assertEqual(event.write_version, index + 1)
            self.assertEqual(event.row_interval, (batch["row_start"], batch["row_end"]))

    def test_untouched_blocks_are_reused_by_identity(self) -> None:
        case = self.cases["spmm_inc_demo"]
        service = SparseIncrementalService(case)
        for index, batch in enumerate(case.write_batches[:3]):
            service.submit_batch(batch, batch_index=index)
        before = service.snapshot_view().blocks
        service.submit_batch(case.write_batches[3], batch_index=3)
        after = service.snapshot_view().blocks
        self.assertIs(before[2], after[2])
        self.assertIs(before[4], after[4])
        self.assertEqual(before[2].digest, after[2].digest)
        self.assertEqual(before[4].digest, after[4].digest)

    def test_duplicate_content_is_accepted_without_result_change(self) -> None:
        case = self.cases["spmm_inc_demo"]
        service = SparseIncrementalService(case)
        for index, batch in enumerate(case.write_batches[:-1]):
            service.submit_batch(batch, batch_index=index)
        before = service.snapshot_view()
        result = service.submit_batch(case.write_batches[-1], batch_index=4)
        after = service.snapshot_view()
        self.assertEqual(result["business_code"], OK)
        self.assertFalse(result["event"]["result_changed"])
        self.assertEqual(
            [block.digest for block in before.blocks],
            [block.digest for block in after.blocks],
        )
        self.assertEqual(after.ledger.version, 5)
        self.assertEqual(after.ledger.accepted[-1]["batch"]["write_version"], 5)

    def test_final_self_check_and_dense_deviation(self) -> None:
        for case_id in ("spmm_inc_demo", "spspmm_inc_demo"):
            with self.subTest(case_id=case_id):
                service = self._run_case(case_id)
                report = service.self_check()
                self.assertEqual(report["business_code"], OK)
                self.assertTrue(report["intervals"]["disjoint"])
                self.assertTrue(report["intervals"]["coverage_complete"])
                self.assertTrue(report["independent_recompute"]["all_results_match"])
                self.assertTrue(report["untouched_checksums"]["byte_identical"])
                self.assertGreater(report["untouched_checksums"]["blocks_checked"], 0)
                self.assertTrue(report["deviation"]["within_tolerance"])
                self.assertLess(report["deviation"]["max_abs_error"], 1e-10)

    def test_invalidated_rows_become_empty(self) -> None:
        case = self.cases["spmm_inc_demo"]
        service = self._run_case("spmm_inc_demo")
        states = service.snapshot_view().row_states
        for row in (6, 7):
            self.assertFalse(states[row].active)
            self.assertEqual(states[row].col_indices, ())
            self.assertEqual(states[row].values, ())
        result = service.materialize()
        self.assertTrue(np.array_equal(result[6], np.zeros(case.n)))
        self.assertTrue(np.array_equal(result[7], np.zeros(case.n)))

    def test_self_check_detects_corrupted_block(self) -> None:
        service = self._run_case("spmm_inc_demo")
        state = service._state
        assert state is not None
        bad_result = state.blocks[0].result.copy()
        bad_result[0, 0] += 1.0
        bad_block = dataclasses.replace(state.blocks[0], result=bad_result)
        service._state = dataclasses.replace(
            state,
            blocks=(bad_block,) + state.blocks[1:],
        )
        with self.assertRaises(BusinessError) as caught:
            service.self_check()
        error = caught.exception
        self.assertEqual(error.business_code, E_INCONSISTENT)
        self.assertEqual(error.case_id, "spmm_inc_demo")
        self.assertEqual(error.details["inconsistency_category"], "block_recompute_mismatch")
        self.assertEqual(error.details["interval"], {"row_start": 0, "row_end": 4})


if __name__ == "__main__":
    unittest.main()
