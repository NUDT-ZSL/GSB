"""High level deterministic render pipeline: inpaint then style transfer."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .config import RenderConfig
from .errors import FormatInvalid
from .images import load_rgb, save_png
from .inpaint import inpaint
from .models import UnifiedProject
from .style import apply_style


@dataclass
class RenderArtifacts:
    inpainted: np.ndarray
    styled: np.ndarray
    inpaint_stats: Dict[str, Any]
    style_stats: Dict[str, Any]


def resolve_path(project_path: Path, referenced: str) -> Path:
    candidate = Path(referenced)
    if candidate.is_absolute():
        return candidate
    direct = project_path.parent / candidate
    if direct.exists():
        return direct.resolve()
    # Deterministic fallbacks for projects copied into nested work dirs.
    # 1) Walk ancestors with the exact relative path stored in the project.
    for ancestor in project_path.parent.parents:
        candidate_path = ancestor / candidate
        if candidate_path.exists():
            return candidate_path.resolve()
    # 2) Only bare media file names (no directory component) may resolve via
    #    the shared samples directory, so workspace projects can never shadow
    #    canonical sample projects by name.
    if len(candidate.parts) == 1:
        for ancestor in project_path.parent.parents:
            samples_path = ancestor / "samples" / candidate.name
            if samples_path.exists():
                return samples_path.resolve()
    return direct.resolve()


def render_project(project: UnifiedProject, project_file: Path,
                   config: RenderConfig,
                   output_dir: Optional[Path] = None,
                   version_map=None) -> RenderArtifacts:
    base = Path(project_file)
    image_path = resolve_path(base, project.image_path)
    ref_path = resolve_path(base, project.style_ref_path)
    source_image = load_rgb(image_path)
    reference = load_rgb(ref_path)

    from dataclasses import replace as _replace
    if not project.style.channel_weights:
        if version_map is None:
            raise FormatInvalid(
                "风格参数缺少分通道权重且未提供版本映射表",
                context={"param_key": "style.channel_weights",
                         "source_path": str(base)})
        project = _replace(
            project,
            style=_replace(
                project.style,
                channel_weights=version_map.weights_for(
                    project.style.intensity)))

    inpaint_result = inpaint(project, source_image, config)
    style_result = apply_style(project, inpaint_result.image, reference, config)

    artifacts = RenderArtifacts(
        inpainted=inpaint_result.image,
        styled=style_result.image,
        inpaint_stats=inpaint_result.stats(),
        style_stats=style_result.stats(),
    )
    if output_dir is not None:
        output_dir = Path(output_dir)
        save_png(output_dir / f"{project.project_id}_inpainted.png",
                 artifacts.inpainted)
        save_png(output_dir / f"{project.project_id}_styled.png",
                 artifacts.styled)
    return artifacts
