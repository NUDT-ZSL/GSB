from __future__ import annotations

import unittest

from sparse_inc.batches import seal_batch
from sparse_inc.codes import (
    E_WRITE_CONFLICT,
    E_WRITE_CORRUPT,
    E_WRITE_RANGE,
    E_WRITE_STALE,
)
from sparse_inc.errors import BusinessError
from sparse_inc.service import SparseIncrementalService

from tests.support import make_sample_directory, replace_batch


class WriteRejectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()
        cls.case = cls.cases["spmm_inc_demo"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def setUp(self) -> None:
        self.service = SparseIncrementalService(self.case)
        self.service.submit_batch(self.case.write_batches[0], batch_index=0)
        self.service.submit_batch(self.case.write_batches[1], batch_index=1)

    def _assert_rejected_without_mutation(self, batch: dict, code: str) -> BusinessError:
        before = self.service.snapshot_view()
        with self.assertRaises(BusinessError) as caught:
            self.service.submit_batch(batch)
        after = self.service.snapshot_view()
        error = caught.exception
        self.assertEqual(error.business_code, code)
        self.assertEqual(error.case_id, self.case.case_id)
        self.assertEqual(before.ledger_snapshot, after.ledger_snapshot)
        self.assertEqual(before.result_snapshot, after.result_snapshot)
        self.assertEqual(before.row_states, after.row_states)
        self.assertEqual(before.blocks, after.blocks)
        return error

    def test_stale_version_is_rejected(self) -> None:
        stale = replace_batch(
            batch_id="stale",
            version=self.service.ledger.version,
            row_start=0,
            row_end=1,
        )
        error = self._assert_rejected_without_mutation(stale, E_WRITE_STALE)
        self.assertEqual(error.segment, "write_version")

    def test_corrupt_checksum_is_rejected(self) -> None:
        corrupt = replace_batch(
            batch_id="corrupt",
            version=self.service.ledger.version + 1,
            row_start=0,
            row_end=1,
        )
        corrupt["rows"][0]["values"][0] += 10.0
        error = self._assert_rejected_without_mutation(corrupt, E_WRITE_CORRUPT)
        self.assertEqual(error.segment, "checksum")

    def test_row_interval_out_of_bounds_is_rejected(self) -> None:
        out_of_range = seal_batch(
            {
                "batch_id": "range",
                "write_version": self.service.ledger.version + 1,
                "row_start": self.case.m,
                "row_end": self.case.m + 1,
                "mode": "replace",
                "rows": [
                    {
                        "row": self.case.m,
                        "col_indices": [0],
                        "values": [1.0],
                    }
                ],
            }
        )
        error = self._assert_rejected_without_mutation(out_of_range, E_WRITE_RANGE)
        self.assertEqual(error.segment, "row_interval")

    def test_duplicate_column_is_rejected(self) -> None:
        conflict = replace_batch(
            batch_id="conflict",
            version=self.service.ledger.version + 1,
            row_start=0,
            row_end=1,
            rows=[
                {
                    "row": 0,
                    "col_indices": [0, 0],
                    "values": [1.0, 2.0],
                }
            ],
        )
        error = self._assert_rejected_without_mutation(conflict, E_WRITE_CONFLICT)
        self.assertIn("col_indices", error.segment or "")

    def test_duplicate_batch_id_is_rejected(self) -> None:
        duplicate_id = replace_batch(
            batch_id=self.case.write_batches[0]["batch_id"],
            version=self.service.ledger.version + 1,
            row_start=0,
            row_end=1,
        )
        error = self._assert_rejected_without_mutation(duplicate_id, E_WRITE_CONFLICT)
        self.assertEqual(error.segment, "batch_id")

    def test_invalidate_with_rows_is_rejected(self) -> None:
        invalid = seal_batch(
            {
                "batch_id": "invalid-invalidate",
                "write_version": self.service.ledger.version + 1,
                "row_start": 0,
                "row_end": 1,
                "mode": "invalidate",
                "rows": [{"row": 0, "col_indices": [], "values": []}],
            }
        )
        self._assert_rejected_without_mutation(invalid, E_WRITE_CONFLICT)


if __name__ == "__main__":
    unittest.main()
