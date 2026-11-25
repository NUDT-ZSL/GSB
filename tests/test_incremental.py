from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from feature_diag.cache import CacheStore
from feature_diag.config import replace_config
from feature_diag.engine import (
    add_cross_slot,
    apply_row_correction,
    change_categorical_codec,
    compute,
    remove_cross_slot,
)
from feature_diag.errors import (
    E_ENCODING_ERROR,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    E_STALE_RESULT,
    EngineError,
)
from feature_diag.generator import prepare_workspace
from feature_diag.sample import read_rows_csv, rows_digest
from feature_diag.snapshot import load_v2


class IncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        prepare_workspace(self.root)
        self.config = load_v2(self.root / "configs" / "v2")
        self.rows = read_rows_csv(self.root / "data" / "sample.csv", self.config)
        self.cache = CacheStore(self.root / "work" / "cache.sqlite", self.config.limits["max_cache_entries"])
        self.cache.reset()
        self.baseline = compute(self.rows, self.config)
        self.cache.commit(
            self.baseline,
            input_digest=rows_digest(self.rows),
            config_digest=self.config.semantic_digest,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_row_correction_reuses_unaffected_rows_and_invalidates_stale_key(self) -> None:
        corrected = apply_row_correction(
            self.rows,
            "r0030",
            numeric_updates={"score": 812.125},
            categorical_updates={"region": "west"},
        )
        updated = compute(corrected, self.config, cache=self.cache, previous=self.baseline)
        self.assertEqual(updated.recomputed_row_ids, ["r0030"])
        self.assertEqual(len(updated.reused_row_ids), len(self.rows) - 1)
        self.assertGreater(len(updated.reused_feature_keys), 700)
        self.assertGreater(len(updated.computed_feature_keys), 0)
        self.assertIn("num.score", updated.affected_feature_ids)
        self.assertIn("cross.income__score#product", updated.affected_feature_ids)
        self.assertTrue(any(feature.startswith("cross.region__segment") for feature in updated.affected_feature_ids))
        self.cache.commit(
            updated,
            input_digest=rows_digest(corrected),
            config_digest=self.config.semantic_digest,
        )
        with self.assertRaises(EngineError) as stale:
            self.cache.load_selection(self.baseline.selection_key)
        self.assertEqual(stale.exception.code, E_STALE_RESULT)
        self.assertNotEqual(updated.selection_key, self.baseline.selection_key)

    def test_codec_switch_reuses_unaffected_feature_entries(self) -> None:
        corrected = apply_row_correction(self.rows, "r0030", categorical_updates={"region": "west"})
        prior = compute(corrected, self.config, cache=self.cache)
        self.cache.commit(
            prior,
            input_digest=rows_digest(corrected),
            config_digest=self.config.semantic_digest,
        )
        switched = change_categorical_codec(self.config, "channel", "target")
        updated = compute(corrected, switched, cache=self.cache)
        self.assertIn("cat.channel#target", updated.affected_feature_ids)
        self.assertNotIn("cat.channel.web", updated.affected_feature_ids)
        self.assertGreater(len(updated.reused_feature_keys), len(updated.computed_feature_keys))
        self.assertEqual(updated.computed_feature_keys, sorted(updated.computed_feature_keys))

    def test_cross_slot_add_and_remove_dependency_propagation(self) -> None:
        corrected = apply_row_correction(self.rows, "r0030", categorical_updates={"region": "west"})
        base = compute(corrected, self.config, cache=self.cache)
        self.cache.commit(
            base,
            input_digest=rows_digest(corrected),
            config_digest=self.config.semantic_digest,
        )
        added_config = add_cross_slot(
            self.config,
            {
                "slot": "region__channel",
                "columns": ["region", "channel"],
                "codec": "one_hot",
                "prior_weight": 5.0,
            },
        )
        added = compute(corrected, added_config, cache=self.cache)
        cross_features = {feature for feature in added.affected_feature_ids if feature.startswith("cross.region__channel")}
        self.assertEqual(len(cross_features), 12)
        self.assertEqual(len(added.computed_feature_keys), 12 * len(self.rows))

        removed_config = remove_cross_slot(added_config, "income__score")
        self.cache.commit(added, input_digest=rows_digest(corrected), config_digest=added_config.semantic_digest)
        removed = compute(corrected, removed_config, cache=self.cache)
        self.assertEqual(removed.computed_feature_keys, [])
        self.assertEqual(removed.recomputed_row_ids, [])
        self.assertNotIn("cross.income__score#product", removed.plan.ids)
        self.assertNotEqual(removed.selection_key, added.selection_key)

    def test_cache_limit_and_stage_busy_are_coded(self) -> None:
        small_cache = CacheStore(self.root / "work" / "small-cache.sqlite", 10)
        small_cache.reset()
        with self.assertRaises(EngineError) as limit:
            small_cache.commit(
                self.baseline,
                input_digest=rows_digest(self.rows),
                config_digest=self.config.semantic_digest,
            )
        self.assertEqual(limit.exception.code, E_RESOURCE_LIMIT)
        self.assertEqual(limit.exception.detail["resource"], "cache")

        busy_cache = CacheStore(self.root / "work" / "busy-cache.sqlite", self.config.limits["max_cache_entries"])
        busy_cache.reset()
        with busy_cache.held_stage():
            with self.assertRaises(EngineError) as busy:
                busy_cache.commit(
                    self.baseline,
                    input_digest=rows_digest(self.rows),
                    config_digest=self.config.semantic_digest,
                )
        self.assertEqual(busy.exception.code, E_STAGE_BUSY)

    def test_unparseable_cache_entry_is_coded(self) -> None:
        row_id = self.baseline.rows[0]["row_id"]
        key = sorted(self.baseline.feature_keys[row_id])[0]
        connection = sqlite3.connect(str(self.cache.path), timeout=0)
        try:
            connection.execute(
                "UPDATE encoding_cache SET value_json='{broken' WHERE cache_key=?",
                (key,),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(EngineError) as caught:
            self.cache.fetch_encoding_values([key])
        self.assertEqual(caught.exception.code, E_ENCODING_ERROR)
        self.assertEqual(caught.exception.detail["cache_key"], key)


if __name__ == "__main__":
    unittest.main()
