"""Sub-pixel coverage domain component for brand packaging previews."""
from .engine import CoverageEngine, EngineState
from .generator import ENGINE_VERSION, SEED, generate_project
from .models import PackagingProject
from .rasterizer import RasterResult, rasterize_polygon, rasterize_strip
from .validation import validate_project

__all__ = [
    "CoverageEngine",
    "EngineState",
    "ENGINE_VERSION",
    "SEED",
    "PackagingProject",
    "RasterResult",
    "rasterize_strip",
    "rasterize_polygon",
    "validate_project",
    "generate_project",
]
