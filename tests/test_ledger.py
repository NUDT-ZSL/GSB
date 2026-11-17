import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from h2workbench.errors import DiagError, E_WINDOW
from h2workbench.ledger import WindowLedger, reconcile_grants


class TestLedger(unittest.TestCase):
    def test_send_and_grant_accounting(self):
        ledger = WindowLedger([1, 2], 100, 250)
        ledger.send(1, 60)
        self.assertEqual(ledger.window(1), 40)
        self.assertEqual(ledger.connection.window, 190)
        ledger.grant(1, 30)
        ledger.grant(0, 60)
        self.assertEqual(ledger.window(1), 70)
        self.assertEqual(ledger.connection.window, 250)

    def test_negative_window_rejected(self):
        ledger = WindowLedger([1], 10, 10)
        with self.assertRaises(DiagError) as ctx:
            ledger.send(1, 11)
        self.assertEqual(ctx.exception.code, E_WINDOW)
        self.assertEqual(ctx.exception.details["stream_id"], 1)

    def test_roundtrip_serialization(self):
        ledger = WindowLedger([1, 2], 100, 200)
        ledger.send(1, 40)
        ledger.grant(2, 15)
        clone = WindowLedger.from_dict(ledger.to_dict())
        self.assertEqual(clone.to_dict(), ledger.to_dict())

    def test_reconcile_ok(self):
        self.assertIsNone(reconcile_grants([(1, 10), (0, 20)],
                                           [(1, 10), (0, 20)]))

    def test_reconcile_first_deviation(self):
        deviation = reconcile_grants([(1, 10), (2, 20), (0, 30)],
                                     [(1, 10), (2, 25), (0, 30)])
        self.assertEqual(deviation["index"], 1)
        self.assertEqual(deviation["stream_id"], 2)
        self.assertEqual(deviation["expected_increment"], 20)
        self.assertEqual(deviation["observed_increment"], 25)
        self.assertEqual(deviation["deviation_bytes"], 5)

    def test_reconcile_length_mismatch(self):
        deviation = reconcile_grants([(1, 10)], [(1, 10), (2, 20)])
        self.assertEqual(deviation["index"], 1)
        self.assertEqual(deviation["stream_id"], 2)


if __name__ == "__main__":
    unittest.main()
