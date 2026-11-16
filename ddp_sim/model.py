"""Deterministic parameter initialisation and gradient shard generation.

All randomness derives from numpy SeedSequence so that the same
(seed, workers, dim, step) combination reproduces bit-identical shards
across runs and processes.
"""

import hashlib

import numpy as np

from .errors import E_INIT, SimError

MIN_WORKERS = 4
MAX_WORKERS = 8
MAX_SEED = 2**32 - 1


def validate_init(seed, workers, dim):
    """Validate init parameters; raise SimError(E_INIT) when illegal."""
    if isinstance(seed, bool) or not isinstance(seed, (int, np.integer)):
        raise SimError(E_INIT, "seed must be an integer", seed=repr(seed))
    if not 0 <= int(seed) <= MAX_SEED:
        raise SimError(E_INIT, "seed out of range [0, 2**32)", seed=int(seed))
    if isinstance(dim, bool) or not isinstance(dim, (int, np.integer)) or int(dim) <= 0:
        raise SimError(E_INIT, "model dim must be a positive integer", dim=repr(dim))
    if isinstance(workers, bool) or not isinstance(workers, (int, np.integer)) or not (
        MIN_WORKERS <= int(workers) <= MAX_WORKERS
    ):
        raise SimError(
            E_INIT,
            f"workers must be in [{MIN_WORKERS}, {MAX_WORKERS}]",
            workers=repr(workers),
        )


def init_params(seed, dim):
    """Deterministic initial parameter vector (float64)."""
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), 0x9A4A]))
    return rng.standard_normal(int(dim)).astype(np.float64) * 0.01


def local_gradient(seed, rank, step, dim):
    """Deterministic local gradient shard for (seed, rank, step)."""
    seq = np.random.SeedSequence([int(seed), int(step), int(rank)])
    rng = np.random.default_rng(seq)
    return rng.standard_normal(int(dim)).astype(np.float64)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def digest_array(arr):
    return digest_bytes(np.ascontiguousarray(arr, dtype=np.float64).tobytes())
