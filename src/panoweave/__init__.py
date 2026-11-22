"""PanoWeave: deterministic multi-view panorama stitching.

The package intentionally depends only on the Python standard library and
NumPy so that it can run in a plain local Windows or POSIX workspace.
"""

from .errors import BusinessError, ErrorCode
from .model import (
    DistortionStage,
    ExposureCurve,
    MigrationItem,
    ParsedScene,
    SceneParameters,
    ViewParameters,
)

__all__ = [
    "BusinessError",
    "DistortionStage",
    "ErrorCode",
    "ExposureCurve",
    "MigrationItem",
    "ParsedScene",
    "SceneParameters",
    "ViewParameters",
]

__version__ = "1.0.0"

