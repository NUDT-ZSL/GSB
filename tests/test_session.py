import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.client import ClientEngine
from h2workbench.errors import E_PROTO
from h2workbench.manifest import load_manifest
from h2workbench.peer import LoopbackPeer
from h2workbench.planner import simulate
from h2workbench.session import Session

SAMPLES = os.path.join(os.path.dirname(__file__), "..", "samples")


class TestSessionLoopback(unittest.TestCase):
    def run_session(self, name, forced=None):
        session = Session.from_file(
            os.path.join(SAMPLES, f"manifest_{name}.json"))
        return session.run(forced_strategy=forced)

    def test_all_samples_reconcile(self):
        for name in ("small", "medium", "large"):
            report = self.run_session(name)
            self.assertTrue(report["progress_consistent"], name)
            self.assertEqual(report["window_reconciliation"], "ok", name)
            self.assertEqual(report["diagnostics"], [], name)

    def test_all_strategies_reconcile(self):
        for strategy in ("arrival", "window_first", "weighted_rr"):
            report = self.run_session("medium", forced=strategy)
            self.assertTrue(report["progress_consistent"], strategy)
            self.assertEqual(report["window_reconciliation"], "ok",
                             strategy)

    def test_observed_grants_match_predicted(self):
        manifest = load_manifest(
            os.path.join(SAMPLES, "manifest_small.json"))
        plan = simulate(manifest, "arrival")
        peer = LoopbackPeer(manifest)
        peer.start()
        port = peer.wait_ready()
        result = ClientEngine(manifest, plan, port).run()
        peer.join(10)
        self.assertEqual([tuple(g) for g in result.grants],
                         [tuple(g) for g in plan.predicted_grants])
        self.assertEqual(result.proto_errors, [])
        self.assertEqual(peer.result.proto_errors, [])

    def test_repeated_runs_byte_identical_plan(self):
        report1 = self.run_session("medium", forced="weighted_rr")
        report2 = self.run_session("medium", forced="weighted_rr")
        self.assertEqual(report1["plan"]["frames"],
                         report2["plan"]["frames"])
        self.assertEqual(report1["plan"]["predicted_grants"],
                         report2["plan"]["predicted_grants"])
        self.assertEqual(report1["runtime"]["observed_grants"],
                         report2["runtime"]["observed_grants"])

    def test_stall_segments_have_monotonic_ms(self):
        report = self.run_session("small", forced="arrival")
        stalls = report["runtime"]["stalls"]
        self.assertGreater(len(stalls), 0)
        for stall in stalls:
            self.assertLessEqual(stall["start_ms"], stall["end_ms"])
            self.assertTrue(stall["streams"])


class TestPeerProtoViolation(unittest.TestCase):
    """A raw misbehaving client must surface E_PROTO from the peer."""

    def test_data_before_headers_is_e_proto(self):
        import socket
        import hyperframe.frame

        manifest = load_manifest(
            os.path.join(SAMPLES, "manifest_small.json"))
        peer = LoopbackPeer(manifest)
        peer.start()
        port = peer.wait_ready()
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        try:
            sock.sendall(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")
            settings = hyperframe.frame.SettingsFrame(0)
            sock.sendall(settings.serialize())
            sock.recv(65535)
            # DATA on stream 1 without HEADERS: stream state violation.
            data = hyperframe.frame.DataFrame(1)
            data.data = b"x" * 16
            data.flags.add("END_STREAM")
            sock.sendall(data.serialize())
            sock.recv(65535)
        except OSError:
            pass
        finally:
            sock.close()
        peer.join(10)
        codes = [e["code"] for e in peer.result.proto_errors]
        self.assertIn(E_PROTO, codes)
        violation = next(e for e in peer.result.proto_errors
                         if e["code"] == E_PROTO)
        self.assertEqual(violation["frame_type"], "DATA")


if __name__ == "__main__":
    unittest.main()
