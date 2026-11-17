import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.ledger import WindowLedger
from h2workbench.manifest import load_manifest
from h2workbench.planner import simulate
from h2workbench.scheduler import STRATEGIES

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


def load(name):
    return load_manifest(os.path.join(SAMPLES, f"manifest_{name}.json"))


class TestPlanner(unittest.TestCase):
    def test_deterministic_byte_identical(self):
        for name in ("small", "medium", "large"):
            manifest = load(name)
            for strategy in STRATEGIES:
                first = simulate(manifest, strategy).to_dict()
                second = simulate(manifest, strategy).to_dict()
                self.assertEqual(first, second,
                                 f"{name}/{strategy} not deterministic")

    def test_frame_size_and_window_limits(self):
        for name in ("small", "medium", "large"):
            manifest = load(name)
            max_frame = manifest["connection"]["max_frame_size"]
            for strategy in STRATEGIES:
                plan = simulate(manifest, strategy)
                ledger = WindowLedger(
                    [s["id"] for s in manifest["streams"]],
                    manifest["connection"]["client_send"]["stream_window"],
                    manifest["connection"]["client_send"]
                            ["connection_window"])
                grants = list(plan.predicted_grants)
                # Apply the setup grant first, then interleave grants as
                # the planner did: replay frames, applying grants whenever
                # the ledger would otherwise go negative.
                for frame in plan.frames:
                    if frame["type"] != "DATA":
                        continue
                    nbytes = frame["bytes"]
                    self.assertLessEqual(nbytes, max_frame)
                    while (ledger.window(frame["stream_id"]) < nbytes
                           or ledger.connection.window < nbytes):
                        self.assertTrue(grants, "plan exceeds all windows")
                        sid, inc = grants.pop(0)
                        ledger.grant(sid, inc)
                    ledger.send(frame["stream_id"], nbytes)
                self.assertGreaterEqual(ledger.connection.window, 0)

    def test_all_streams_fully_sent(self):
        manifest = load("medium")
        for strategy in STRATEGIES:
            plan = simulate(manifest, strategy)
            sent = {}
            for frame in plan.frames:
                if frame["type"] == "DATA":
                    sent[frame["stream_id"]] = (
                        sent.get(frame["stream_id"], 0) + frame["bytes"])
            for stream in manifest["streams"]:
                self.assertEqual(sent.get(stream["id"], 0),
                                 stream["body_bytes"])

    def test_stall_segments_detected(self):
        manifest = load("small")  # grant_increment > stream window
        plan = simulate(manifest, "arrival")
        self.assertGreater(len(plan.stalls), 0)
        for stall in plan.stalls:
            self.assertLessEqual(stall["start_ms"], stall["end_ms"])
            self.assertTrue(stall["streams"])

    def test_strategies_differ_in_frame_order(self):
        manifest = load("medium")
        orders = set()
        for strategy in STRATEGIES:
            plan = simulate(manifest, strategy)
            orders.add(tuple(f["stream_id"] for f in plan.frames
                             if f["type"] == "DATA"))
        self.assertGreater(len(orders), 1,
                           "strategies produced identical schedules")


if __name__ == "__main__":
    unittest.main()
