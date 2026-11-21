"""Deterministic image-text generation quality evaluation."""

from .config import EvaluatorConfig
from .engine import QualityEvaluator
from .errors import DomainError
from .samples import EvaluationSample, build_demo_samples, make_sample, render_image, tokenize
from .snapshot import EvaluationSnapshot, evolve_snapshot, make_snapshot

__all__ = [
    "DomainError",
    "EvaluationSample",
    "EvaluationSnapshot",
    "EvaluatorConfig",
    "QualityEvaluator",
    "build_demo_samples",
    "evolve_snapshot",
    "make_sample",
    "make_snapshot",
    "render_image",
    "tokenize",
]

__version__ = "1.0.0"

