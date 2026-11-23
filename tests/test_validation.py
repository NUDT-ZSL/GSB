"""业务要求 8：输入与配置校验链路、结构化业务码与字段/case 归因。"""

import unittest

import numpy as np

from rope_attn.config import validate_array_dims, validate_tasks
from rope_attn.errors import (
    DimensionMismatchError,
    DuplicateCaseError,
    OddHeadDimError,
    UnsupportedDtypeError,
    ValidationError,
)
from rope_attn.generate_tasks import DEFAULT_TASKS
from tests._helpers import make_task


class ValidationTest(unittest.TestCase):
    def _raw(self, **overrides):
        task = make_task(**overrides).to_dict()
        task.update(overrides)
        return task

    def test_duplicate_case_id(self):
        rows = [self._raw(), self._raw(seed=99)]
        with self.assertRaises(DuplicateCaseError) as ctx:
            validate_tasks(rows)
        self.assertEqual(ctx.exception.field, "case_id")
        self.assertEqual(ctx.exception.case_id, "case_test")

    def test_odd_head_dim(self):
        with self.assertRaises(OddHeadDimError) as ctx:
            validate_tasks([self._raw(head_dim=7)])
        self.assertEqual(ctx.exception.code, "E_ODD_HEAD_DIM")
        self.assertEqual(ctx.exception.field, "head_dim")

    def test_base_theta_out_of_range(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_tasks([self._raw(base_theta=0.5)])
        self.assertEqual(ctx.exception.field, "base_theta")
        with self.assertRaises(ValidationError):
            validate_tasks([self._raw(base_theta=1e12)])

    def test_pos_scale_non_positive(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_tasks([self._raw(pos_scale=0.0)])
        self.assertEqual(ctx.exception.field, "pos_scale")

    def test_block_size_out_of_range(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_tasks([self._raw(block_size=99)])
        self.assertEqual(ctx.exception.field, "block_size")

    def test_unsupported_dtype(self):
        with self.assertRaises(UnsupportedDtypeError) as ctx:
            validate_tasks([self._raw(dtype="float16")])
        self.assertEqual(ctx.exception.field, "dtype")

    def test_unsupported_variant(self):
        with self.assertRaises(ValidationError) as ctx:
            validate_tasks([self._raw(pos_encoding_variant="alibi")])
        self.assertEqual(ctx.exception.field, "pos_encoding_variant")

    def test_dimension_mismatch_on_replacement(self):
        task = make_task()
        wrong = np.zeros((task.seq_len, task.num_heads, task.head_dim - 1),
                         dtype=np.float32)
        with self.assertRaises(DimensionMismatchError) as ctx:
            validate_array_dims(task, "K_replace", wrong)
        self.assertEqual(ctx.exception.code, "E_DIM_MISMATCH")
        self.assertEqual(ctx.exception.field, "K_replace")

    def test_default_tasks_are_valid(self):
        tasks = validate_tasks(DEFAULT_TASKS)
        self.assertEqual(len(tasks), 3)


if __name__ == "__main__":
    unittest.main()
