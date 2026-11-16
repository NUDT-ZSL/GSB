import threading
import time
import unittest

from oswb.uploader import Scheduler


class SlowFakeClient:
    def __init__(self, delay=0.05):
        self.delay = delay
        self.inflight = 0
        self.peak = 0
        self.stored = {}
        self._lock = threading.Lock()

    def upload_chunk(self, session_id, index, data):
        with self._lock:
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
        time.sleep(self.delay)
        with self._lock:
            self.inflight -= 1
            self.stored.setdefault((session_id, index), data)
        return {"session_id": session_id, "index": index,
                "deduplicated": False, "digest": "ok"}

    def get_session(self, session_id):
        return {"chunks": [
            {"index": index, "uploaded": (session_id, index) in self.stored,
             "digest": "ok"}
            for index in range(20)]}


class SchedulerTests(unittest.TestCase):
    def test_global_and_session_limits_are_respected(self):
        fake = SlowFakeClient()
        scheduler = Scheduler(fake, global_concurrency=3, session_quota=2)
        jobs = [{"session_id": "s1", "index": index, "data": b"x"}
                for index in range(12)]
        scheduler.run(jobs)
        self.assertLessEqual(fake.peak, 2)
        self.assertEqual(len(fake.stored), 12)
        self.assertTrue(all(item["ok"] for item in scheduler.attempts))

    def test_two_sessions_share_global_cap(self):
        fake = SlowFakeClient(delay=0.02)
        scheduler = Scheduler(fake, global_concurrency=4, session_quota=3)
        jobs = [{"session_id": "s1", "index": index, "data": b"x"}
                for index in range(8)]
        jobs += [{"session_id": "s2", "index": index, "data": b"y"}
                 for index in range(8)]
        scheduler.run(jobs)
        self.assertLessEqual(fake.peak, 4)
        self.assertEqual(len(fake.stored), 16)


class SessionIsolationTests(unittest.TestCase):
    def test_same_index_in_two_sessions_is_not_deduplicated(self):
        # Regression: inflight dedup must key on (session_id, index), otherwise
        # two sessions sharing index numbers deadlock waiting on each other.
        fake = SlowFakeClient(delay=0.01)
        scheduler = Scheduler(fake, global_concurrency=4, session_quota=2)
        jobs = [{"session_id": "s1", "index": index, "data": b"a"}
                for index in range(6)]
        jobs += [{"session_id": "s2", "index": index, "data": b"b"}
                 for index in range(6)]
        scheduler.run(jobs)
        self.assertEqual(len(fake.stored), 12)
        self.assertTrue(all(item["ok"] for item in scheduler.attempts))
        self.assertEqual(
            sorted({key[0] for key in fake.stored}), ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
