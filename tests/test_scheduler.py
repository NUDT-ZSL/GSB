import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mocap_audio import scheduler as scheduler_mod
from mocap_audio.errors import FormatError
from mocap_audio.session import load_session
from mocap_audio.state import BoundarySignaler, StateStore
from tests.test_integration import _devices, _session_dict, _write_device_wavs


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "wav"), exist_ok=True)
        self.session_path = os.path.join(self.tmp, "session.json")
        devices = _devices()
        _write_device_wavs(self.tmp, devices)
        with open(self.session_path, "w", encoding="utf-8") as fh:
            json.dump(_session_dict(devices, concurrency=2), fh)

    def test_first_failure_cancels_remaining_and_keeps_completed(self):
        session = load_session(self.session_path)
        store = StateStore(session.resolve("state"), "snap-sched")
        store.prepare()
        original = scheduler_mod._process_segment
        calls = {"count": 0}

        def flaky(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FormatError("模拟解码失败", refs=["REF", "S1"])
            return original(*args, **kwargs)

        with mock.patch.object(scheduler_mod, "_process_segment",
                               side_effect=flaky):
            with self.assertRaises(FormatError) as ctx:
                scheduler_mod.run_segment_jobs(
                    session, 48000, store, BoundarySignaler(), {})
        self.assertEqual(ctx.exception.code, "E_FORMAT")
        # 首段失败：错误按声明顺序上抛；同时在途的 D1 合法结果仍保留
        persisted = os.listdir(os.path.join(store.root, "segments"))
        self.assertTrue(any("D1__S2" in name for name in persisted))
        self.assertEqual(ctx.exception.refs, ["REF", "S1"])

    def test_late_failure_keeps_earlier_completed_segment(self):
        session = load_session(self.session_path)
        store = StateStore(session.resolve("state"), "snap-sched3")
        store.prepare()
        original = scheduler_mod._process_segment

        def flaky(*args, **kwargs):
            refs = args[-1] if args else kwargs.get("refs")
            if refs and refs.get("device_id") == "D1":
                raise FormatError("模拟第二段失败", refs=["D1", "S2"])
            return original(*args, **kwargs)

        with mock.patch.object(scheduler_mod, "_process_segment",
                               side_effect=flaky):
            with self.assertRaises(FormatError):
                scheduler_mod.run_segment_jobs(
                    session, 48000, store, BoundarySignaler(), {})
        persisted = os.listdir(os.path.join(store.root, "segments"))
        self.assertTrue(any("REF__S1" in name for name in persisted))

    def test_budget_admission_waits_in_declaration_order(self):
        session = load_session(self.session_path)
        session.memory_budget_bytes = 2000000
        store = StateStore(session.resolve("state"), "snap-sched2")
        store.prepare()
        planned, results = scheduler_mod.run_segment_jobs(
            session, 48000, store, BoundarySignaler(), {})
        self.assertEqual([seg.segment_id for _, seg in planned],
                         ["S1", "S2"])
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r is not None for r in results))


if __name__ == "__main__":
    unittest.main()

