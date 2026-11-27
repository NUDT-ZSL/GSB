"""Deterministic SDF-based sign-language hand rendering toolkit."""

from .animation import AnimationSampler, PoseSolver, SkinResult, SkinnedHand
from .collision import IntersectionDetector
from .deformation import DeformationAuditor
from .project import (
    CameraSpec,
    KeyMoment,
    MeshData,
    PrimitiveSpec,
    Project,
    RenderSettings,
    load_key_moments,
    load_project,
)
from .sdf import PrimitiveInstance, SDFScene
from .tracer import (
    TRACE_ERR_HIT_BEYOND_FAR_PLANE,
    TRACE_ERR_MAX_STEPS,
    TRACE_HIT,
    TRACE_MISS,
    TRACE_STATUS_NAMES,
    SphereTracer,
    render_frame,
)

__all__ = [
    "AnimationSampler",
    "CameraSpec",
    "DeformationAuditor",
    "IntersectionDetector",
    "KeyMoment",
    "MeshData",
    "PoseSolver",
    "PrimitiveInstance",
    "PrimitiveSpec",
    "Project",
    "RenderSettings",
    "SDFScene",
    "SkinResult",
    "SkinnedHand",
    "SphereTracer",
    "TRACE_ERR_HIT_BEYOND_FAR_PLANE",
    "TRACE_ERR_MAX_STEPS",
    "TRACE_HIT",
    "TRACE_MISS",
    "TRACE_STATUS_NAMES",
    "load_key_moments",
    "load_project",
    "render_frame",
]

