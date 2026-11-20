from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from leasecoord.db import answer_json
from leasecoord.engine import Engine
from leasecoord.errors import BusinessError

from .helpers import load_sample, run_to_failover


class ElectionTests(unittest.TestCase):
    def test_failover_precision_pair_and_verification(self) -> None:
        with TemporaryDirectory() as temp:
            engine = run_to_failover(Path(temp) / "ws")
            status = engine.status(clock="1000")
            self.assertEqual(status["leader"], "charlie")
            self.assertTrue(status["precision_sensitive"])
            self.assertEqual(status["precision_pairs"][0]["node_a"], "charlie")
            self.assertEqual(status["precision_pairs"][0]["node_b"], "alpha")
            self.assertEqual(status["precision_pairs"][0]["gap_ms"], "0.0002")
            self.assertEqual(status["first_rejection"]["node"], "bravo")
            self.assertEqual(status["first_rejection"]["code"], "E_LEASE_EXPIRED")
            self.assertGreaterEqual(len(status["precision_pairs"][0]["error_source_chain"]), 4)
            self.assertTrue(engine.verify()["verified"])

    def test_boundary_view_marks_both_nodes_without_mutation(self) -> None:
        with TemporaryDirectory() as temp:
            engine = run_to_failover(Path(temp) / "ws")
            before = engine.inspect_database()
            first = engine.status(clock="1849.9999")
            second = engine.status(clock="1849.9999")
            self.assertEqual(answer_json(first), answer_json(second))
            self.assertEqual(first["watermark_ms"], "1000")
            self.assertEqual(first["boundary_candidates"], ["alpha", "charlie"])
            self.assertEqual(first["leader"], "charlie")
            flags = {
                node["name"]: (node["boundary_candidate"], node["precision_sensitive"])
                for node in first["nodes"]
            }
            self.assertEqual(flags["alpha"], (True, True))
            self.assertEqual(flags["charlie"], (True, True))
            self.assertEqual(engine.inspect_database(), before)

    def test_same_clock_election_is_idempotent(self) -> None:
        with TemporaryDirectory() as temp:
            engine, _, _ = load_sample(Path(temp) / "ws")
            first = engine.elect(clock="0")
            second = engine.elect(clock="0")
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            self.assertEqual(first["election_id"], second["election_id"])
            self.assertEqual(engine.inspect_database()["election_id"], first["election_id"])

    def test_renew_authorization_and_data_version(self) -> None:
        with TemporaryDirectory() as temp:
            engine, _, _ = load_sample(Path(temp) / "ws")
            engine.elect(clock="0")
            with self.assertRaises(BusinessError) as caught:
                engine.renew(node="alpha", session_epoch=0, clock="100")
            self.assertEqual(caught.exception.code, "E_STALE_SESSION")
            with self.assertRaises(BusinessError) as caught:
                engine.renew(node="missing", session_epoch=1, clock="100")
            self.assertEqual(caught.exception.code, "E_UNKNOWN_NODE")
            with self.assertRaises(BusinessError) as caught:
                engine.renew(
                    node="alpha",
                    session_epoch=1,
                    clock="100",
                    lease_generation=9,
                )
            self.assertEqual(caught.exception.code, "E_STALE_LEASE")
            with self.assertRaises(BusinessError) as caught:
                engine.renew(
                    node="alpha",
                    session_epoch=1,
                    clock="100",
                    data_version=1,
                )
            self.assertEqual(caught.exception.code, "E_STALE_DATA_VERSION")
            with self.assertRaises(BusinessError) as caught:
                engine.renew(node="alpha", session_epoch=1, clock="1001")
            self.assertEqual(caught.exception.code, "E_LEASE_EXPIRED")

    def test_renew_is_idempotent_and_rejection_is_audited(self) -> None:
        with TemporaryDirectory() as temp:
            engine, _, _ = load_sample(Path(temp) / "ws")
            engine.elect(clock="0")
            first = engine.renew(node="alpha", session_epoch=1, clock="100")
            second = engine.renew(node="alpha", session_epoch=1, clock="100")
            self.assertFalse(first["idempotent"])
            self.assertTrue(second["idempotent"])
            database = engine.inspect_database()
            self.assertEqual(database["renewals"], 1)
            with self.assertRaises(BusinessError):
                engine.renew(node="alpha", session_epoch=0, clock="100")
            with engine.db.transaction(immediate=False) as connection:
                event = connection.execute(
                    """
                    SELECT code FROM audit_events
                    WHERE kind='renew_rejected' AND node_name='alpha'
                    ORDER BY event_id LIMIT 1
                    """
                ).fetchone()
            self.assertEqual(event["code"], "E_STALE_SESSION")

    def test_clock_jump_and_rewind_do_not_advance_state(self) -> None:
        with TemporaryDirectory() as temp:
            engine, _, _ = load_sample(Path(temp) / "ws")
            engine.elect(clock="0")
            before = engine.inspect_database()
            with self.assertRaises(BusinessError) as caught:
                engine.elect(clock="10001")
            self.assertEqual(caught.exception.code, "E_CLOCK_JUMP")
            self.assertEqual(engine.inspect_database(), before)
            engine.renew(node="alpha", session_epoch=1, clock="100")
            with self.assertRaises(BusinessError) as caught:
                engine.elect(clock="99")
            self.assertEqual(caught.exception.code, "E_CLOCK_REWIND")
            self.assertEqual(engine.inspect_database()["watermark_ms"], "100")


if __name__ == "__main__":
    unittest.main()
