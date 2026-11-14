"""Lightmap second-UV seam light-leak checker.

The public entry point is :func:`run_analysis`.  Given a bake settings JSON
and an output directory it writes one annotated PNG per lightmap plus a
``seam_result.json`` report.
"""

from .report import RESULT_FILENAME, run_analysis

__all__ = ["RESULT_FILENAME", "run_analysis"]
__version__ = "1.0.0"
