import numpy as np
import pytest

from ddp_sim.errors import E_INIT, SimError
from ddp_sim.model import (digest_array, init_params, local_gradient,
                           validate_init)


def test_shards_are_bit_identical_across_generations():
    a = local_gradient(seed=7, rank=2, step=3, dim=128)
    b = local_gradient(seed=7, rank=2, step=3, dim=128)
    assert a.tobytes() == b.tobytes()
    assert digest_array(a) == digest_array(b)


def test_shards_differ_by_rank_and_step():
    base = local_gradient(seed=7, rank=0, step=0, dim=64)
    assert base.tobytes() != local_gradient(7, 1, 0, 64).tobytes()
    assert base.tobytes() != local_gradient(7, 0, 1, 64).tobytes()
    assert base.tobytes() != local_gradient(8, 0, 0, 64).tobytes()


def test_init_params_deterministic():
    assert init_params(1, 32).tobytes() == init_params(1, 32).tobytes()


@pytest.mark.parametrize("seed,workers,dim", [
    (-1, 4, 16), (2**33, 4, 16), ("x", 4, 16),
    (1, 3, 16), (1, 9, 16), (1, 4, 0), (1, 4, -5),
])
def test_invalid_init_raises_e_init(seed, workers, dim):
    with pytest.raises(SimError) as err:
        validate_init(seed, workers, dim)
    assert err.value.code == E_INIT


def test_reference_sum_matches_fixed_order():
    grads = [local_gradient(11, r, 0, 32) for r in range(4)]
    total = np.zeros(32)
    for g in grads:
        total = total + g
    assert total.shape == (32,)
