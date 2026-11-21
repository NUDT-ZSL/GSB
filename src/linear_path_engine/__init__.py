"""Local deterministic regularized linear-model path training engine."""

import os

# BLAS threading must not affect reduction order or reproducibility.
for _thread_env in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_thread_env, "1")

from .engine import TrainingEngine, run_training
from .errors import EngineError

__all__ = ["EngineError", "TrainingEngine", "run_training"]
__version__ = "0.1.0"

