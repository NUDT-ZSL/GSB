from __future__ import annotations

import tempfile
import unittest

from mvcc_app.errors import BusinessError, ErrorCode
from tests.helpers import (
    delete,
    insert,
    make_engine,
    range_read,
    range_results,
    row,
    session,
    update,
)


class MVCCVisibilityTests(unittest.TestCase):
    def test_repeatable_read_differs_from_read_committed_and_own_writes_merge(self) -> None:
        sessions = [
            session(
                100,
                900,
                "repeatable_read",
                980,
                [
                    insert(1, 910, row(1, "alpha", 100)),
                    insert(2, 920, row(2, "beta", 200)),
                ],
            ),
            session(
                101,
                1000,
                "repeatable_read",
                1300,
                [
                    range_read(
                        1,
                        1010,
                        "101-initial",
                        where=[{"column": "id", "op": "eq", "value": 1}],
                    ),
                    update(2, 1020, {"id": 1}, {"balance": 150}),
                    range_read(
                        3,
                        1030,
                        "101-own",
                        where=[{"column": "id", "op": "eq", "value": 1}],
                    ),
                    insert(4, 1040, row(3, "gamma", 240)),
                    update(5, 1050, {"id": 3}, {"balance": 250}),
                ],
            ),
            session(
                102,
                1050,
                "read_committed",
                1400,
                [
                    range_read(
                        1,
                        1060,
                        "102-before",
                        where=[{"column": "id", "op": "eq", "value": 1}],
                    ),
                    range_read(
                        2,
                        1310,
                        "102-after",
                        where=[{"column": "id", "op": "in", "value": [1, 3]}],
                        order_by=["id"],
                    ),
                ],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path

            engine, store, _, _ = make_engine(Path(directory), sessions)
            engine.replay_sessions(engine.store.load_head()[1]["sessions"])
            _, state = store.load_head()
            before = range_results(state, "102-before")
            after = range_results(state, "102-after")
            self.assertEqual(before["rows"][0]["values"]["balance"], 100)
            self.assertEqual(before["snapshot"]["snapshot_watermark_ms"], 980)
            self.assertEqual(before["snapshot"]["view_clock_ms"], 1060)
            self.assertIn(101, before["snapshot"]["active_transactions"])
            self.assertEqual(
                [(item["key"]["id"], item["values"]["balance"]) for item in after["rows"]],
                [(1, 150), (3, 250)],
            )
            self.assertEqual(after["snapshot"]["snapshot_watermark_ms"], 1300)
            self.assertEqual(after["snapshot"]["view_clock_ms"], 1310)
            self.assertNotIn(101, after["snapshot"]["active_transactions"])

            own = range_results(state, "101-own")
            self.assertEqual(own["rows"][0]["values"]["balance"], 150)
            self.assertEqual(own["rows"][0]["visibility"]["basis"], "current_transaction_write")
            id3_versions = [
                version
                for version in state["versions"].values()
                if version["row_key"] == '{"id":3}'
            ]
            self.assertEqual(len(id3_versions), 1)
            self.assertEqual(id3_versions[0]["create_txn_id"], 101)
            self.assertEqual(id3_versions[0]["merge_count"], 1)

            view_a = engine._build_view(state, 102, "read_committed", 1310)
            reordered = dict(reversed(list(state["transactions"].items())))
            original_transactions = state["transactions"]
            try:
                state["transactions"] = reordered
                view_b = engine._build_view(state, 102, "read_committed", 1310)
            finally:
                state["transactions"] = original_transactions
            self.assertEqual(view_a, view_b)
            self.assertEqual(view_a["active_txns"], sorted(view_a["active_txns"]))

    def test_rollback_delete_reinsert_and_gc(self) -> None:
        sessions = [
            session(
                100,
                900,
                "repeatable_read",
                980,
                [
                    insert(1, 910, row(1, "alpha", 100)),
                    insert(2, 920, row(2, "beta", 200)),
                ],
            ),
            session(
                101,
                1000,
                "repeatable_read",
                1100,
                [
                    delete(1, 1010, {"id": 1}),
                    insert(2, 1020, row(1, "alpha-rebuilt", 999)),
                ],
            ),
            session(
                102,
                1000,
                "repeatable_read",
                1200,
                [
                    range_read(
                        1,
                        1030,
                        "102-old-version",
                        where=[{"column": "id", "op": "eq", "value": 1}],
                    )
                ],
            ),
            session(
                103,
                1130,
                "read_committed",
                1250,
                [
                    range_read(
                        1,
                        1140,
                        "103-new-version",
                        where=[{"column": "id", "op": "eq", "value": 1}],
                    )
                ],
            ),
            session(
                104,
                1260,
                "repeatable_read",
                1300,
                [
                    insert(1, 1270, row(4, "delta", 400, "south")),
                    update(2, 1280, {"id": 2}, {"balance": 250}),
                ],
                outcome="rolled_back",
            ),
            session(
                105,
                1310,
                "read_committed",
                1400,
                [
                    range_read(
                        1,
                        1320,
                        "105-after-rollback",
                        where=[{"column": "id", "op": "in", "value": [2, 4]}],
                        order_by=["id"],
                    )
                ],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path

            engine, store, _, normalized = make_engine(Path(directory), sessions)
            engine.replay_sessions(normalized)
            _, state = store.load_head()
            old = range_results(state, "102-old-version")
            new = range_results(state, "103-new-version")
            self.assertEqual(old["rows"][0]["values"]["name"], "alpha")
            self.assertEqual(old["rows"][0]["visibility"]["deletion_basis"], "active_delete_ignored")
            self.assertEqual(new["rows"][0]["values"]["name"], "alpha-rebuilt")

            after_rollback = range_results(state, "105-after-rollback")
            self.assertEqual(
                [(item["key"]["id"], item["values"]["balance"]) for item in after_rollback["rows"]],
                [(2, 200)],
            )
            rolled_back = [
                version
                for version in state["versions"].values()
                if version["create_txn_id"] == 104
            ]
            self.assertEqual(len(rolled_back), 2)
            self.assertTrue(all(version["state"] == "rolled_back" for version in rolled_back))
            rolled_back_ids = {version["version_id"] for version in rolled_back}

            gc_result = engine.run_gc(clock_ms=1500)
            self.assertGreaterEqual(gc_result["collected_count"], 2)
            _, after_gc = store.load_head()
            self.assertFalse(rolled_back_ids.intersection(after_gc["versions"]))
            self.assertEqual(after_gc["gc_runs"]["gc:1500:1"]["retained_count"], gc_result["retained_count"])
            self.assertEqual(
                after_gc["gc"]["collected_total"],
                gc_result["collected_count"],
            )

    def test_lost_update_is_rejected(self) -> None:
        sessions = [
            session(
                100,
                900,
                "repeatable_read",
                980,
                [insert(1, 910, row(1, "alpha", 100))],
            ),
            session(
                101,
                1000,
                "repeatable_read",
                1200,
                [update(1, 1100, {"id": 1}, {"balance": 150})],
            ),
            session(
                102,
                1050,
                "repeatable_read",
                1400,
                [update(1, 1300, {"id": 1}, {"balance": 175})],
            ),
        ]
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path

            engine, store, _, normalized = make_engine(Path(directory), sessions)
            with self.assertRaises(BusinessError) as caught:
                engine.replay_sessions(normalized)
            self.assertEqual(caught.exception.code, ErrorCode.WRITE_CONFLICT)
            _, state = store.load_head()
            txn_102_versions = [
                version
                for version in state["versions"].values()
                if version["create_txn_id"] == 102
            ]
            self.assertEqual(txn_102_versions, [])
            self.assertEqual(state["transactions"][102]["status"], "active")


if __name__ == "__main__":
    unittest.main()

