"""Deterministic geometry fusion and quadric-style edge-collapse simplification.

The fused mesh is constructed on a deterministic voxel grid derived from the
registered point cloud.  Simplification collapses edges in a fixed ordering:
the lowest collapse cost first, with ties broken by ascending endpoint vertex
indices, so identical inputs always produce byte-identical meshes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Set, Tuple

import numpy as np

from .errors import FaceBudgetError


@dataclass
class Mesh:
    vertices: np.ndarray
    faces: np.ndarray


@dataclass
class GeometryResult:
    mesh: Mesh
    pre_simplify_face_count: int
    post_simplify_face_count: int
    pre_simplify_vertex_count: int
    post_simplify_vertex_count: int
    face_budget: int
    overage: int
    geometry_mode: str
    downgraded: bool

    def to_payload(self) -> Dict[str, object]:
        return {
            "pre_simplify_face_count": int(self.pre_simplify_face_count),
            "post_simplify_face_count": int(self.post_simplify_face_count),
            "pre_simplify_vertex_count": int(self.pre_simplify_vertex_count),
            "post_simplify_vertex_count": int(self.post_simplify_vertex_count),
            "face_budget": int(self.face_budget),
            "overage": int(self.overage),
            "geometry_mode": self.geometry_mode,
            "downgraded": bool(self.downgraded),
        }


def estimate_geometry_units(face_count: int, vertex_count: int) -> int:
    """Resident memory units for mesh intermediates (scaled bytes estimate)."""
    return int(vertex_count * 24 + face_count * 12)


def _voxel_index(points: np.ndarray, resolution: int) -> np.ndarray:
    scaled = (points - points.min(axis=0)) / (np.ptp(points, axis=0) + 1e-12)
    indices = np.floor(scaled * (resolution - 1)).astype(np.int64)
    return np.clip(indices, 0, resolution - 1)


def _cube_faces(origin: np.ndarray, size: float) -> Tuple[np.ndarray, np.ndarray]:
    """Eight cube vertices and twelve outward-oriented triangles."""
    offsets = np.array(
        [
            [0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
            [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1],
        ],
        dtype=np.float64,
    )
    vertices = origin[None, :] + offsets * size
    faces = np.array(
        [
            [0, 3, 2], [0, 2, 1],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [2, 3, 7], [2, 7, 6],
            [1, 2, 6], [1, 6, 5],
            [0, 4, 7], [0, 7, 3],
        ],
        dtype=np.int64,
    )
    return vertices, faces


def fuse_geometry(points: np.ndarray, geometry_mode: str,
                  resolution: int) -> Mesh:
    """Build the fused voxel mesh from the point cloud."""
    voxel_indices = _voxel_index(points, resolution)
    occupied: Set[Tuple[int, int, int]] = {
        tuple(values) for values in voxel_indices
    }
    if geometry_mode == "shell":
        occupied = {tuple(np.maximum(values, 1)) for values in occupied}
    elif geometry_mode != "solid":
        raise ValueError(f"Unknown geometry_mode: {geometry_mode!r}")

    lower = points.min(axis=0)
    extent = float(np.ptp(points)) + 1e-12
    size = extent / resolution
    all_vertices: List[np.ndarray] = []
    all_faces: List[np.ndarray] = []
    for voxel in sorted(occupied):
        origin = lower + np.asarray(voxel, dtype=np.float64) * size
        cube_vertices, cube_faces = _cube_faces(origin, size)
        offset = sum(part.shape[0] for part in all_vertices)
        all_vertices.append(cube_vertices)
        all_faces.append(cube_faces + offset)
    vertices = np.ascontiguousarray(np.vstack(all_vertices), dtype=np.float64)
    faces = np.ascontiguousarray(np.vstack(all_faces), dtype=np.int64)
    return Mesh(vertices=vertices, faces=faces)


def _edge_list(faces: np.ndarray) -> List[Tuple[int, int]]:
    edges: Set[Tuple[int, int]] = set()
    for face in faces:
        for index in range(3):
            a, b = int(face[index]), int(face[(index + 1) % 3])
            edges.add((min(a, b), max(a, b)))
    return sorted(edges)


def _collapse_cost(vertices: np.ndarray, edge: Tuple[int, int]) -> float:
    return float(np.linalg.norm(vertices[edge[0]] - vertices[edge[1]]))


def simplify_mesh(mesh: Mesh, target_face_budget: int) -> Mesh:
    """Deterministic edge-collapse simplification.

    Each collapse merges one edge into its midpoint, removes every incident
    triangle and drops faces that become degenerate.
    """
    if target_face_budget < 4:
        raise FaceBudgetError(
            "target_face_budget below the minimum supported value (4)",
            details={"target_face_budget": int(target_face_budget)},
        )

    vertices = np.array(mesh.vertices, dtype=np.float64, copy=True)
    faces = np.array(mesh.faces, dtype=np.int64, copy=True)

    while faces.shape[0] > target_face_budget:
        edges = _edge_list(faces)
        scored = sorted(
            ((_collapse_cost(vertices, edge), edge) for edge in edges),
            key=lambda item: (item[0], item[1][0], item[1][1]),
        )
        collapsed = False
        for _, (keep, remove) in scored:
            candidate_faces = np.array(faces, copy=True)
            candidate_faces[candidate_faces == remove] = keep
            non_degenerate = np.array(
                [len(set(face)) == 3 for face in candidate_faces], dtype=bool
            )
            if not non_degenerate.any():
                continue
            # Midpoint placement keeps geometry deterministic and symmetric.
            vertices[keep] = 0.5 * (vertices[keep] + vertices[remove])
            faces = candidate_faces[non_degenerate]
            collapsed = True
            break
        if not collapsed:
            break

    used_vertices, inverse = np.unique(faces, return_inverse=True)
    compact_vertices = np.ascontiguousarray(vertices[used_vertices])
    compact_faces = np.ascontiguousarray(inverse.reshape(faces.shape).astype(np.int64))
    return Mesh(vertices=compact_vertices, faces=compact_faces)


def process_geometry(registration_result, geometry_mode: str,
                     target_face_budget: int, grid_resolution: int,
                     downgraded: bool = False) -> GeometryResult:
    fused = fuse_geometry(
        registration_result.points, geometry_mode, grid_resolution
    )
    pre_faces = int(fused.faces.shape[0])
    simplified = simplify_mesh(fused, target_face_budget)
    post_faces = int(simplified.faces.shape[0])
    overage = max(0, post_faces - target_face_budget)
    if overage > 0:
        raise FaceBudgetError(
            "simplified mesh exceeds target_face_budget",
            details={
                "face_count": post_faces,
                "target_face_budget": int(target_face_budget),
                "overage": int(overage),
            },
        )
    return GeometryResult(
        mesh=simplified,
        pre_simplify_face_count=pre_faces,
        post_simplify_face_count=post_faces,
        pre_simplify_vertex_count=int(fused.vertices.shape[0]),
        post_simplify_vertex_count=int(simplified.vertices.shape[0]),
        face_budget=int(target_face_budget),
        overage=0,
        geometry_mode=geometry_mode,
        downgraded=downgraded,
    )
