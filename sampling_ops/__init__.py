"""Deterministic TopK and sampling operators.

The public entry points are intentionally small:

* :func:`topk` - one of three exact TopK routes.
* :func:`sample` - temperature/top-p/deterministic categorical sampling.
* :func:`decide_route` - reproducible scale/resource route decision.
* :func:`adjudicate_bias` - float64 full-sort reference comparison.
* :func:`run_task` - the unified four-stage operator entry point.
* :func:`self_check` - candidate replay and cross-route checks.
"""

from .bias import adjudicate_bias
from .decision import decide_route
from .errors import BusinessError
from .pipeline import run_task, self_check
from .sampling import SampleResult, sample
from .topk import TopKResult, topk, topk_all_routes

__all__ = [
    "BusinessError",
    "SampleResult",
    "TopKResult",
    "adjudicate_bias",
    "decide_route",
    "run_task",
    "sample",
    "self_check",
    "topk",
    "topk_all_routes",
]

__version__ = "1.0.0"

