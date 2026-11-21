"""Deterministic staged GEMM/BMM pipeline."""

from .config import PipelineConfig
from .engine import PipelineEngine, RunReport
from .errors import PipelineError
from .jobs import JobSpec, generate_sample_jobs, load_jobs, validate_job

__all__ = [
    "JobSpec",
    "PipelineConfig",
    "PipelineEngine",
    "PipelineError",
    "RunReport",
    "generate_sample_jobs",
    "load_jobs",
    "validate_job",
]

__version__ = "1.0.0"
