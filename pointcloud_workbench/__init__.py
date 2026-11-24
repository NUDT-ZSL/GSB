"""Offline, deterministic point-cloud block rendering workbench."""

from .errors import BusinessError, EXIT_DIGEST, EXIT_INTERRUPTED, EXIT_VALIDATION
from .pipeline import RenderOptions, render_task
from .samplegen import generate_samples

__all__ = [
    "BusinessError",
    "EXIT_DIGEST",
    "EXIT_INTERRUPTED",
    "EXIT_VALIDATION",
    "RenderOptions",
    "generate_samples",
    "render_task",
]

__version__ = "1.0.0"

