import threading
import unittest

from kfdiff.errors import KFError
from tests.helpers import make_studio


class StageLockTests(unittest.TestCase):
    def test_conflicting_submission_gets_e_stage_busy_without_waiting(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)

        outcome = {}

        def submit():
            try:
                studio.generate("clip_0002", parallel=False)
                outcome["code"] = None
            except KFError as exc:
                outcome["code"] = exc.code

        with studio.governor.stage("test-holder"):
            thread = threading.Thread(target=submit)
            thread.start()
            thread.join(timeout=5)
        self.assertEqual(outcome["code"], "E_STAGE_BUSY")

    def test_multiple_queries_run_concurrently(self):
        studio, _ = make_studio()
        studio.generate("clip_0001", parallel=False)
        barrier = threading.Barrier(6)

        def worker():
            with studio.governor.query():
                barrier.wait(timeout=5)
                studio.query_ready("clip_0001")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertGreaterEqual(studio.governor.max_concurrent_queries_observed, 6)


class ResourceLimitTests(unittest.TestCase):
    def test_clip_memory_cap_returns_e_resource_limit(self):
        studio, _ = make_studio({"memory": {"max_clip_resident_bytes": 1}})
        with self.assertRaises(KFError) as ctx:
            studio.generate("clip_0001", parallel=False)
        self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")

    def test_parallel_slots_reject_policy(self):
        studio, _ = make_studio({
            "memory": {"max_parallel_jobs": 1, "on_parallel_limit": "reject"}
        })
        occupied = studio.governor.occupy_all_slots_for_test()
        try:
            with self.assertRaises(KFError) as ctx:
                studio.generate("clip_0001")
            self.assertEqual(ctx.exception.code, "E_RESOURCE_LIMIT")
        finally:
            for _ in range(occupied):
                studio.governor.release_job_slot()

    def test_parallel_slots_serial_degradation(self):
        studio, _ = make_studio({
            "memory": {"max_parallel_jobs": 1, "on_parallel_limit": "serialize"}
        })
        occupied = studio.governor.occupy_all_slots_for_test()
        try:
            result = studio.generate("clip_0001")
            self.assertTrue(result["snapshot_version"])
            self.assertTrue(
                any(d["category"] == "parallel_to_serial"
                    for d in studio.governor.degradations)
            )
        finally:
            for _ in range(occupied):
                studio.governor.release_job_slot()


if __name__ == "__main__":
    unittest.main()
