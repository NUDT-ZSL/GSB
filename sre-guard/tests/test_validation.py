import json
import os
import tempfile
import unittest

from sre_guard.errors import E_EVENT, E_POLICY, GovernanceError
from sre_guard.events import load_events
from sre_guard.policy import load_policy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ValidationTests(unittest.TestCase):
    def test_valid_policy_loads(self):
        policy = load_policy(os.path.join(ROOT, "config", "policy.json"))
        self.assertGreaterEqual(policy.version, 1)
        self.assertIn("payment-api", policy.services)

    def _write(self, suffix, content):
        handle = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                             encoding="utf-8")
        handle.write(content)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name

    def test_policy_threshold_out_of_range(self):
        with open(os.path.join(ROOT, "config", "policy.json"), encoding="utf-8") as h:
            data = json.load(h)
        data["services"]["payment-api"]["breaker"]["failure_threshold_percent"] = 101
        path = self._write(".json", json.dumps(data))
        with self.assertRaises(GovernanceError) as ctx:
            load_policy(path)
        self.assertEqual(ctx.exception.code, E_POLICY)
        self.assertTrue(any("failure_threshold_percent" in i.path for i in ctx.exception.issues))

    def test_policy_duplicate_degrade_target(self):
        with open(os.path.join(ROOT, "config", "policy.json"), encoding="utf-8") as h:
            data = json.load(h)
        chain = data["services"]["payment-api"]["degrade_chain"]
        chain.append(dict(chain[0]))
        path = self._write(".json", json.dumps(data))
        with self.assertRaises(GovernanceError) as ctx:
            load_policy(path)
        self.assertEqual(ctx.exception.code, E_POLICY)

    def test_event_timestamp_regression(self):
        content = "\n".join([
            json.dumps({"timestamp_ms": 100, "service_id": "s", "result": "success", "latency_ms": 1}),
            json.dumps({"timestamp_ms": 90, "service_id": "s", "result": "failure", "latency_ms": 2}),
        ])
        path = self._write(".ndjson", content)
        with self.assertRaises(GovernanceError) as ctx:
            load_events(path)
        self.assertEqual(ctx.exception.code, E_EVENT)
        self.assertEqual(ctx.exception.issues[0].line, 2)

    def test_event_bad_result_and_negative_latency(self):
        content = json.dumps({"timestamp_ms": 1, "service_id": "s",
                              "result": "unknown", "latency_ms": -1})
        path = self._write(".ndjson", content)
        with self.assertRaises(GovernanceError) as ctx:
            load_events(path)
        self.assertEqual(ctx.exception.code, E_EVENT)
        paths = {i.path for i in ctx.exception.issues}
        self.assertIn("result", paths)
        self.assertIn("latency_ms", paths)

    def test_valid_events_load(self):
        events = load_events(os.path.join(ROOT, "data", "events.ndjson"))
        self.assertGreater(len(events), 0)
        timestamps = [e.timestamp_ms for e in events]
        self.assertEqual(timestamps, sorted(timestamps))


if __name__ == "__main__":
    unittest.main()
