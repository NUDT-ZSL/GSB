"""matgen - 确定性程序化材质纹理生成工具.

公共入口均为纯函数或显式注入依赖的引擎类，便于在库与命令行两种形态下复用。
"""

from .errors import BusinessError, ErrorCode
from .model import ChannelSpec, GenConfig, MaterialSpec
from .noise import fbm_field, fbm_point
from .channels import build_channels
from .tiling import ChunkPlan, plan_chunks, render_tile, stitch
from .persistence import (
    build_chunk_digest,
    build_version_id,
    load_valid_chunk,
    reset_material_state,
    write_chunk,
)
from .engine import GenerationEngine, GenerationResult

__all__ = [
    "BusinessError",
    "ErrorCode",
    "ChannelSpec",
    "GenConfig",
    "MaterialSpec",
    "fbm_field",
    "fbm_point",
    "build_channels",
    "ChunkPlan",
    "plan_chunks",
    "render_tile",
    "stitch",
    "build_chunk_digest",
    "build_version_id",
    "load_valid_chunk",
    "reset_material_state",
    "write_chunk",
    "GenerationEngine",
    "GenerationResult",
]

__version__ = "1.0.0"
