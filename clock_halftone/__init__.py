"""Deterministic clock-movement halftone teaching-board kernel."""

from __future__ import annotations

__version__ = "1.0.0"


def run_pipeline(*args, **kwargs):
    """Lazy entry point so low-level modules remain importable during setup."""
    from .api import run_pipeline as _run_pipeline

    return _run_pipeline(*args, **kwargs)


__all__ = ["run_pipeline", "__version__"]
