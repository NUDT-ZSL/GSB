from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .heightfield import GlobalHeightField
from .io_utils import canonical_digest


@dataclass(frozen=True)
class ChunkSpec:
    index: int
    ty: int
    tx: int
    x0: int
    y0: int
    x1: int
    y1: int

    @property
    def width(self) -> int:
        return self.x1 - self.x0

    @property
    def height(self) -> int:
        return self.y1 - self.y0

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    @property
    def chunk_id(self) -> str:
        return f"chunk_r{self.ty:03d}_c{self.tx:03d}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "index": self.index,
            "tile_y": self.ty,
            "tile_x": self.tx,
            "pixel_bounds": {
                "x0": self.x0,
                "y0": self.y0,
                "x1_exclusive": self.x1,
                "y1_exclusive": self.y1,
            },
            "width": self.width,
            "height": self.height,
        }


def build_chunks(width: int, height: int, tile_width: int, tile_height: int) -> List[ChunkSpec]:
    chunks: List[ChunkSpec] = []
    index = 0
    ty = 0
    for y0 in range(0, height, tile_height):
        tx = 0
        y1 = min(height, y0 + tile_height)
        for x0 in range(0, width, tile_width):
            x1 = min(width, x0 + tile_width)
            chunks.append(ChunkSpec(index, ty, tx, x0, y0, x1, y1))
            index += 1
            tx += 1
        ty += 1
    return chunks


def camera_digest(scene: Dict[str, Any]) -> str:
    camera = scene["camera"]
    return canonical_digest(
        {
            "position": camera["position"],
            "target": camera["target"],
            "up": camera["up"],
            "fov_y_degrees": camera["fov_y_degrees"],
            "near": camera["near"],
            "far": camera["far"],
        }
    )


def lighting_digest(scene: Dict[str, Any]) -> str:
    light = scene["lighting"]
    return canonical_digest(
        {
            "direction": light["direction"],
            "ambient": light["ambient"],
            "intensity": light["intensity"],
        }
    )


def march_digest(scene: Dict[str, Any]) -> str:
    march = scene["ray_march"]
    resolution = scene["resolution"]
    return canonical_digest(
        {
            "step_world": march["step_world"],
            "max_steps": march["max_steps"],
            "refinement_steps": march["refinement_steps"],
            "normal_radius_cells": march["normal_radius_cells"],
            "width": resolution["width"],
            "height": resolution["height"],
            "tile_width": resolution["tile_width"],
            "tile_height": resolution["tile_height"],
            "pixel_density": resolution["pixel_density"],
        }
    )


def compute_global_version(scene: Dict[str, Any], heightfield: GlobalHeightField) -> str:
    """Hash all height content plus camera, lighting and matching step settings."""

    return canonical_digest(
        {
            "format": "terrain-baker-global-version/v1",
            "heightfield": {
                "content_digest": heightfield.content_digest,
                "origin": scene["heightfield"]["origin"],
                "world_scale": scene["heightfield"]["world_scale"],
                "global_rows": heightfield.rows,
                "global_cols": heightfield.cols,
                "tile_hashes": heightfield.tile_hashes,
            },
            "camera": scene["camera"],
            "lighting": scene["lighting"],
            "ray_march": scene["ray_march"],
            "resolution": scene["resolution"],
        }
    )


def height_range_digest(heightfield: GlobalHeightField, dep_range: Dict[str, int]) -> str:
    r0 = int(dep_range.get("row_start", 0))
    r1 = int(dep_range.get("row_end_exclusive", 0))
    c0 = int(dep_range.get("col_start", 0))
    c1 = int(dep_range.get("col_end_exclusive", 0))
    if r1 <= r0 or c1 <= c0:
        return canonical_digest({"empty": True})
    values = heightfield.heights_world[r0:r1, c0:c1]
    import hashlib

    values_hash = hashlib.sha256(
        np_as_contiguous_float64_le(values)
    ).hexdigest()
    return canonical_digest(
        {
            "row_start": r0,
            "row_end_exclusive": r1,
            "col_start": c0,
            "col_end_exclusive": c1,
            "shape": list(values.shape),
            "world_height_float64_sha256": values_hash,
        }
    )


def np_as_contiguous_float64_le(values) -> bytes:
    import numpy as np

    return np.ascontiguousarray(values, dtype="<f8").tobytes()


def compute_dependency_digest(
    scene: Dict[str, Any],
    heightfield: GlobalHeightField,
    chunk: ChunkSpec,
    dep_range: Dict[str, int],
) -> str:
    return canonical_digest(
        {
            "format": "terrain-baker-chunk-dependency/v1",
            "chunk_id": chunk.chunk_id,
            "pixel_bounds": chunk.to_dict()["pixel_bounds"],
            "height_dependency": height_range_digest(heightfield, dep_range),
            "camera_digest": camera_digest(scene),
            "lighting_digest": lighting_digest(scene),
            "march_digest": march_digest(scene),
        }
    )


def dependency_graph_descriptor(
    chunk: ChunkSpec,
    dep_range: Dict[str, int],
    dependency_digest: str,
) -> Dict[str, Any]:
    height_node = (
        f"heightfield:rows[{dep_range['row_start']}:{dep_range['row_end_exclusive']}]"
        f",cols[{dep_range['col_start']}:{dep_range['col_end_exclusive']}]"
    )
    camera_node = "camera:global"
    light_node = "lighting:global"
    march_node = "ray_march:step_world"
    output_node = f"output:{chunk.chunk_id}"
    return {
        "dependency_digest": dependency_digest,
        "height_range": dep_range,
        "nodes": [output_node, height_node, camera_node, light_node, march_node],
        "edges": [
            {"from": height_node, "to": output_node, "relation": "bilinear_samples_and_global_normals"},
            {"from": camera_node, "to": output_node, "relation": "ray_origin_direction"},
            {"from": light_node, "to": output_node, "relation": "lambert_transfer"},
            {"from": march_node, "to": output_node, "relation": "deterministic_ray_stepping"},
        ],
        "propagation": {
            "terrain_subregion_edit": "invalidate iff half-open grid ranges intersect",
            "lighting_change": "invalidate all chunks",
            "camera_change": "invalidate all chunks",
            "step_change": "invalidate all chunks",
        },
    }


def ranges_intersect(edit_region: Dict[str, int], dep_range: Optional[Dict[str, int]]) -> bool:
    if not dep_range:
        return False
    return max(int(edit_region["row_start"]), int(dep_range.get("row_start", 0))) < min(
        int(edit_region["row_end_exclusive"]),
        int(dep_range.get("row_end_exclusive", 0)),
    ) and max(int(edit_region["col_start"]), int(dep_range.get("col_start", 0))) < min(
        int(edit_region["col_end_exclusive"]),
        int(dep_range.get("col_end_exclusive", 0)),
    )


def classify_invalidation(
    chunks: Sequence[ChunkSpec],
    cache_metadata: Dict[str, Optional[Dict[str, Any]]],
    current_dependency_digests: Dict[str, Optional[str]],
    *,
    mode: str,
    edit_regions: Optional[Sequence[Dict[str, int]]] = None,
    changed_keys: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Return per-chunk invalidation decisions and machine-readable reasons."""

    changed = list(changed_keys or [])
    results: List[Dict[str, Any]] = []
    for chunk in chunks:
        metadata = cache_metadata.get(chunk.chunk_id)
        reasons: List[str] = []
        invalid = False
        status = "reused"

        if mode == "full_reference":
            invalid = True
            status = "rerendered"
            reasons.append("full_reference_requested")
        elif metadata is None:
            invalid = True
            status = "rerendered"
            reasons.append("missing_or_invalid_committed_chunk_state")
        elif mode == "lighting":
            invalid = True
            status = "rerendered"
            reasons.append("lighting_parameter_changed")
            if changed:
                reasons.append("changed_keys:" + ",".join(changed))
        elif mode == "camera":
            invalid = True
            status = "rerendered"
            reasons.append("camera_parameter_changed")
            if changed:
                reasons.append("changed_keys:" + ",".join(changed))
        elif mode == "ray_step":
            invalid = True
            status = "rerendered"
            reasons.append("ray_march_settings_changed")
            if changed:
                reasons.append("changed_keys:" + ",".join(changed))
        elif mode == "terrain_edit":
            dep_range = metadata.get("height_dependency_range")
            if not edit_regions:
                reasons.append("terrain_edit_region_not_declared")
                invalid = True
            else:
                hits = [region for region in edit_regions if ranges_intersect(region, dep_range)]
                if hits:
                    invalid = True
                    reasons.append("terrain_edit_intersects_height_dependency")
                    for edit_region in hits:
                        reasons.append(
                            "edit_rows[{0}:{1}]_cols[{2}:{3}]".format(
                                edit_region["row_start"],
                                edit_region["row_end_exclusive"],
                                edit_region["col_start"],
                                edit_region["col_end_exclusive"],
                            )
                        )
            if not invalid:
                expected_digest = current_dependency_digests.get(chunk.chunk_id)
                if expected_digest and metadata.get("dependency_digest") != expected_digest:
                    invalid = True
                    reasons.append("dependency_digest_changed_without_region_intersection")
            if invalid:
                status = "rerendered"
            else:
                reasons.extend(
                    [
                        "terrain_edit_does_not_intersect_height_dependency",
                        "dependency_digest_match",
                        "committed_payload_valid",
                    ]
                )
        else:  # initial/auto/restart
            expected_digest = current_dependency_digests.get(chunk.chunk_id)
            if expected_digest and metadata.get("dependency_digest") != expected_digest:
                invalid = True
                status = "rerendered"
                reasons.append("dependency_digest_mismatch")
            else:
                reasons.extend(["committed_payload_valid", "dependency_digest_match"])

        results.append(
            {
                "chunk_id": chunk.chunk_id,
                "index": chunk.index,
                "tile_y": chunk.ty,
                "tile_x": chunk.tx,
                "invalid": invalid,
                "status": status,
                "reasons": reasons,
                "cached_global_version": (metadata or {}).get("global_version"),
                "height_dependency_range": (metadata or {}).get("height_dependency_range"),
                "global_grid_coverage": (metadata or {}).get("global_grid_coverage"),
                "dependency_digest": (metadata or {}).get("dependency_digest"),
            }
        )
    return results


def build_invalidation_matrix(
    decisions: Sequence[Dict[str, Any]],
    edit_regions: Optional[Sequence[Dict[str, int]]],
) -> Dict[str, Any]:
    terrain_chunks: List[Dict[str, Any]] = []
    lighting_chunks: List[Dict[str, Any]] = []
    camera_chunks: List[Dict[str, Any]] = []
    for decision in decisions:
        dep_range = decision.get("height_dependency_range")
        terrain_hit = any(ranges_intersect(region, dep_range) for region in (edit_regions or []))
        terrain_chunks.append(
            {
                "chunk_id": decision["chunk_id"],
                "invalid": terrain_hit,
                "reasons": (
                    ["terrain_edit_intersects_height_dependency"]
                    if terrain_hit
                    else ["terrain_edit_does_not_intersect_height_dependency"]
                ),
            }
        )
        lighting_chunks.append(
            {
                "chunk_id": decision["chunk_id"],
                "invalid": True,
                "reasons": ["lighting_parameter_changed:all_lambert_terms"],
            }
        )
        camera_chunks.append(
            {
                "chunk_id": decision["chunk_id"],
                "invalid": True,
                "reasons": ["camera_parameter_changed:all_primary_rays"],
            }
        )
    return {
        "terrain_subregion_edit": {
            "edit_regions": list(edit_regions or []),
            "invalid_chunk_ids": [item["chunk_id"] for item in terrain_chunks if item["invalid"]],
            "chunks": terrain_chunks,
        },
        "lighting_change": {
            "invalid_chunk_ids": "ALL",
            "chunks": lighting_chunks,
        },
        "camera_change": {
            "invalid_chunk_ids": "ALL",
            "chunks": camera_chunks,
        },
    }
