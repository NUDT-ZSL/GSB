"""Deterministic Poisson image-fusion toolkit.

The public API is intentionally small.  ``generate_sample`` creates a
reproducible sample, ``fuse_images`` performs a full Poisson fusion and
``replace_foreground`` performs an incremental replacement with a full
recomputation consistency check.
"""

from .errors import FusionError
from .pipeline import (
    FusionParameters,
    fuse_image_files,
    replace_foreground_files,
)
from .sample_gen import generate_sample

__all__ = [
    "FusionError",
    "FusionParameters",
    "fuse_image_files",
    "generate_sample",
    "replace_foreground_files",
]

__version__ = "1.0.0"
