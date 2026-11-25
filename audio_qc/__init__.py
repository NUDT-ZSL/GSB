"""Deterministic local audio quality-control component."""

from .codes import BusinessCode
from .errors import QCError
from .pipeline import PipelineRequest, run_pipeline
from .samples import generate_bundle, write_sample_bundle
from .verify import verify_serial_vs_oneshot

__version__ = "0.1.0"

__all__ = [
    "BusinessCode",
    "PipelineRequest",
    "QCError",
    "generate_bundle",
    "run_pipeline",
    "verify_serial_vs_oneshot",
    "write_sample_bundle",
    "__version__",
]
