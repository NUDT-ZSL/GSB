from __future__ import annotations

import unittest

from tests.helpers import SAMPLES

from shardrouter.config import load_config
from shardrouter.session import RouterSession
from shardrouter.ui import build_ui_model
from shardrouter.verification import canonical_text


class UITests(unittest.TestCase):
    def test_view_model_is_stable_ordered_and_non_mutating(self) -> None:
        session = RouterSession(load_config(SAMPLES / "config.json"))
        session.route("orders", "tenant_id=7 AND region='east'")
        cache_size = len(session.cache)
        history_size = len(session.history)
        first = build_ui_model(session)
        second = build_ui_model(session)
        self.assertEqual(canonical_text(first), canonical_text(second))
        self.assertEqual(len(session.cache), cache_size)
        self.assertEqual(len(session.history), history_size)
        self.assertEqual([row["name"] for row in first["shard_rules"]], ["ledger", "orders", "sessions"])
        self.assertEqual(
            [row["route_unit"] for row in first["target_mapping"]],
            sorted(row["route_unit"] for row in first["target_mapping"]),
        )
        self.assertEqual(first["dependency_edges"], [
            {"dependency": "orders_lookup", "dependent": "orders_rollup", "display": "orders_rollup <- orders_lookup"},
            {"dependency": "orders_region", "dependent": "orders_rollup", "display": "orders_rollup <- orders_region"},
        ])


if __name__ == "__main__":
    unittest.main()
