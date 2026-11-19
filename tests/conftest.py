from __future__ import annotations

import numpy as np
import pytest

from qconv.models import Job


@pytest.fixture
def small_job() -> Job:
    return Job(
        job_id="unit_small",
        input_shape=(1, 2, 6, 6),
        kernel_shape=(3, 2, 3, 3),
        stride=(1, 1),
        padding=(1, 1),
        quant_scale=0.02,
        zero_point=-2,
        acc_dtype="int32",
        seed=12345,
        priority=50,
    )


@pytest.fixture
def second_job() -> Job:
    return Job(
        job_id="unit_second",
        input_shape=(1, 1, 5, 5),
        kernel_shape=(2, 1, 3, 3),
        stride=(1, 1),
        padding=(0, 0),
        quant_scale=0.04,
        zero_point=1,
        acc_dtype="int32",
        seed=67890,
        priority=40,
    )

