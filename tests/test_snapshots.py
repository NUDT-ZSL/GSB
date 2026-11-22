from __future__ import annotations

import copy
import unittest

from sparse_inc.codes import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, OK
from sparse_inc.errors import BusinessError
from sparse_inc.service import SparseIncrementalService

from tests.support import make_sample_directory


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp, cls.cases = make_sample_directory()
        cls.case = cls.cases["spmm_inc_demo"]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_rollback_and_corruption_do_not_mutate_current_state(self) -> None:
        service = SparseIncrementalService(self.case)
        for index, batch in enumerate(self.case.write_batches[:2]):
            service.submit_batch(batch, batch_index=index)
        old_bundle = service.export_bundle()
        for index, batch in enumerate(self.case.write_batches[2:], start=2):
            service.submit_batch(batch, batch_index=index)
        before = service.snapshot_view()

        with self.assertRaises(BusinessError) as rollback:
            service.import_bundle(old_bundle)
        self.assertEqual(rollback.exception.business_code, E_SNAPSHOT_ROLLBACK)
        after_rollback = service.snapshot_view()
        self.assertEqual(before.ledger_snapshot, after_rollback.ledger_snapshot)
        self.assertEqual(before.result_snapshot, after_rollback.result_snapshot)

        corrupt = copy.deepcopy(service.export_bundle())
        corrupt["results"]["checksum"] = "sha256:" + "f" * 64
        with self.assertRaises(BusinessError) as corrupt_error:
            service.import_bundle(corrupt)
        error = corrupt_error.exception
        self.assertEqual(error.business_code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(error.details["snapshot_category"], "row_results")
        self.assertEqual(error.details["snapshot_version"], 5)
        after_corrupt = service.snapshot_view()
        self.assertEqual(before.ledger_snapshot, after_corrupt.ledger_snapshot)
        self.assertEqual(before.result_snapshot, after_corrupt.result_snapshot)

    def test_ledger_snapshot_corruption_is_categorized(self) -> None:
        service = SparseIncrementalService(self.case)
        service.submit_batch(self.case.write_batches[0], batch_index=0)
        corrupt = copy.deepcopy(service.export_bundle())
        corrupt["ledger"]["payload"]["row_owners"][0]["active"] = False
        with self.assertRaises(BusinessError) as caught:
            service.import_bundle(corrupt)
        error = caught.exception
        self.assertEqual(error.business_code, E_SNAPSHOT_CORRUPT)
        self.assertEqual(error.details["snapshot_category"], "write_ledger")
        self.assertEqual(error.details["snapshot_version"], 1)

    def test_newer_snapshot_replaces_whole_state(self) -> None:
        source = SparseIncrementalService(self.case)
        for index, batch in enumerate(self.case.write_batches[:3]):
            source.submit_batch(batch, batch_index=index)
        newer = source.export_bundle()

        target = SparseIncrementalService(self.case)
        target.submit_batch(self.case.write_batches[0], batch_index=0)
        self.assertEqual(target.ledger.version, 1)
        result = target.import_bundle(newer)
        self.assertEqual(result["business_code"], OK)
        self.assertEqual(target.ledger.version, 3)
        self.assertEqual(target.export_bundle(), newer)

    def test_same_snapshot_import_is_idempotent(self) -> None:
        service = SparseIncrementalService(self.case)
        service.submit_batch(self.case.write_batches[0], batch_index=0)
        bundle = service.export_bundle()
        before = service.snapshot_view()
        result = service.import_bundle(bundle)
        self.assertEqual(result["business_code"], OK)
        after = service.snapshot_view()
        self.assertEqual(before.ledger_snapshot, after.ledger_snapshot)
        self.assertEqual(before.result_snapshot, after.result_snapshot)


if __name__ == "__main__":
    unittest.main()
