"""输入与参数注入校验测试。"""

import unittest

from dp_bench.errors import (ConfigInvalidError, ModelShapeMismatchError,
                             SampleMalformedError)
from dp_bench.loader import parse_sample_line, validate_config
from dp_bench.model import ACTIONS, TransitionModel


def base_config():
    return {
        "max_workers": 4,
        "max_steps_per_sentence": 64,
        "global_step_budget": 10000,
        "per_sentence_quota": 64,
        "timeout_observe_ms": 50,
        "batch_size": 4,
    }


def base_model(dim=32):
    import numpy as np
    rng = np.random.default_rng(1)
    return {
        "feature_dim": dim,
        "hash_buckets": dim,
        "word_hash": {"<NULL>": 0},
        "pos_hash": {"<NULL>": 0, "NN": 1},
        "weights": {a: rng.normal(0, 1, dim).tolist() for a in ACTIONS},
    }


class ValidationTest(unittest.TestCase):
    def test_config_missing_field(self):
        cfg = base_config()
        del cfg["batch_size"]
        with self.assertRaises(ConfigInvalidError) as ctx:
            validate_config(cfg)
        self.assertEqual(ctx.exception.code, "E_CONFIG_INVALID")

    def test_config_non_positive_workers(self):
        cfg = base_config()
        cfg["max_workers"] = 0
        with self.assertRaises(ConfigInvalidError):
            validate_config(cfg)

    def test_config_non_positive_budget(self):
        cfg = base_config()
        cfg["global_step_budget"] = -1
        with self.assertRaises(ConfigInvalidError):
            validate_config(cfg)

    def test_model_dim_bucket_mismatch(self):
        params = base_model()
        params["hash_buckets"] = 31
        with self.assertRaises(ModelShapeMismatchError) as ctx:
            TransitionModel(params)
        self.assertEqual(ctx.exception.code, "E_MODEL_SHAPE_MISMATCH")

    def test_model_weight_shape_mismatch(self):
        params = base_model()
        params["weights"]["SHIFT"] = params["weights"]["SHIFT"][:-1]
        with self.assertRaises(ModelShapeMismatchError):
            TransitionModel(params)

    def test_sample_missing_pos(self):
        with self.assertRaises(SampleMalformedError) as ctx:
            parse_sample_line("S99 经济 建设/NN", 1)
        self.assertEqual(ctx.exception.code, "E_SAMPLE_MALFORMED")

    def test_sample_only_id(self):
        with self.assertRaises(SampleMalformedError):
            parse_sample_line("S99", 2)

    def test_sample_empty_piece(self):
        with self.assertRaises(SampleMalformedError):
            parse_sample_line("S99 /NN 好/JJ", 3)

    def test_valid_sample_parses(self):
        sid, tokens = parse_sample_line(
            "S01 经济/NN 建设/NN 取得/VV", 1)
        self.assertEqual(sid, "S01")
        self.assertEqual([t.index for t in tokens], [1, 2, 3])
        self.assertEqual(tokens[0].word, "经济")
        self.assertEqual(tokens[0].pos, "NN")


if __name__ == "__main__":
    unittest.main()
