"""材质逐方向光照预览工具。

用法见 ``python -m material_preview --help``。
"""

from __future__ import annotations

from .manifest import Manifest, ManifestError, MaterialSpec, angle_problem, load_manifest
from .render import render_direction
from .runner import REPORT_NAME, UsageError, run
from .textures import MaterialTextureError, load_material_textures

__all__ = [
    "Manifest",
    "ManifestError",
    "MaterialSpec",
    "MaterialTextureError",
    "REPORT_NAME",
    "UsageError",
    "angle_problem",
    "load_manifest",
    "load_material_textures",
    "render_direction",
    "run",
]
