"""Deterministic local focus-stacking kernel (NumPy only).

The package implements a complete, repeatable focus-fusion pipeline:

* deterministic analytic sample generation (:mod:`focus_stack.samples`)
* metadata loading and validation (:mod:`focus_stack.io_stack`)
* per-pixel sharpness, selection, smoothing and weighted fusion
  (:mod:`focus_stack.sharpness`, :mod:`focus_stack.consistency`,
  :mod:`focus_stack.fusion`)
* an independent pixel-loop reference fuser (:mod:`focus_stack.reference`)
* region based incremental updates (:mod:`focus_stack.incremental`)
* quality reporting (:mod:`focus_stack.reporting`)
* versioned persistence / resume (:mod:`focus_stack.persistence`)

All floating point work is performed in ``float64`` so that reruns are bit for
bit reproducible. No network access or third party dataset is required.
"""

from .config import CoreConfig

__all__ = ["CoreConfig"]
__version__ = "1.0.0"
