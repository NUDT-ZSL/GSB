"""文物模型几何：OBJ 读取、轴对齐包围盒、材质分区与顶点法线。"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class MaterialGroup:
    name: str
    face_indices: list = field(default_factory=list)   # 面在 faces 列表中的下标
    vertex_indices: list = field(default_factory=list)  # 去重后的全局顶点下标


@dataclass
class Model:
    vertices: np.ndarray                  # (N,3) 世界坐标顶点
    faces: np.ndarray                     # (M,3) 三角形顶点下标（0 基）
    groups: dict                          # 材质名 -> MaterialGroup
    aabb_min: np.ndarray
    aabb_max: np.ndarray
    center: np.ndarray
    half_extents: np.ndarray
    vertex_normals: np.ndarray           # (N,3)

    def to_payload(self, precision: int = 6) -> dict:
        return {
            "vertex_count": int(self.vertices.shape[0]),
            "face_count": int(self.faces.shape[0]),
            "material_groups": sorted(self.groups.keys()),
            "aabb_min": np.round(self.aabb_min, precision).tolist(),
            "aabb_max": np.round(self.aabb_max, precision).tolist(),
            "center": np.round(self.center, precision).tolist(),
            "half_extents": np.round(self.half_extents, precision).tolist(),
        }


def parse_obj(text: str) -> Model:
    """解析简化 OBJ 文本。支持 v / f（三角或四边形，四边形自动三角化）/ usemtl。"""
    vertices = []
    faces = []
    groups: dict[str, MaterialGroup] = {}
    current_group = MaterialGroup(name="default")
    groups["default"] = current_group

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        keyword = parts[0]
        if keyword == "v":
            vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif keyword == "f":
            # 兼容 v / v//vn / v/vt/vn，取斜杠前的位置索引（OBJ 为 1 基）
            idx = [int(p.split("/")[0]) - 1 for p in parts[1:]]
            if len(idx) == 3:
                triangles = [idx]
            elif len(idx) == 4:
                triangles = [[idx[0], idx[1], idx[2]], [idx[0], idx[2], idx[3]]]
            else:
                raise ValueError(f"仅支持三角/四边面，收到 {len(idx)} 顶点面")
            for tri in triangles:
                face_index = len(faces)
                faces.append(tri)
                current_group.face_indices.append(face_index)
                for vi in tri:
                    if vi not in current_group.vertex_indices:
                        current_group.vertex_indices.append(vi)
        elif keyword == "usemtl":
            name = parts[1]
            if name not in groups:
                groups[name] = MaterialGroup(name=name)
            current_group = groups[name]

    verts = np.asarray(vertices, dtype=np.float64)
    face_arr = np.asarray(faces, dtype=np.int64)
    if verts.size == 0:
        raise ValueError("OBJ 中没有任何顶点")
    if not groups["default"].face_indices and len(groups) > 1:
        groups.pop("default")

    aabb_min = verts.min(axis=0)
    aabb_max = verts.max(axis=0)
    center = (aabb_min + aabb_max) / 2.0
    half_extents = (aabb_max - aabb_min) / 2.0
    normals = _compute_vertex_normals(verts, face_arr)

    return Model(
        vertices=verts,
        faces=face_arr,
        groups=groups,
        aabb_min=aabb_min,
        aabb_max=aabb_max,
        center=center,
        half_extents=half_extents,
        vertex_normals=normals,
    )


def _compute_vertex_normals(vertices: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """面积加权的面法线累加，再归一化得到顶点法线。"""
    normals = np.zeros_like(vertices)
    for tri in faces:
        p0, p1, p2 = vertices[tri[0]], vertices[tri[1]], vertices[tri[2]]
        face_normal = np.cross(p1 - p0, p2 - p0)
        for vi in tri:
            normals[vi] += face_normal
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    lengths[lengths == 0.0] = 1.0
    return normals / lengths


def model_from_arrays(vertices, faces, groups=None) -> Model:
    """从顶点/面片数组直接构建模型（HTTP JSON 场景使用）。"""
    verts = np.asarray(vertices, dtype=np.float64)
    face_arr = np.asarray(faces, dtype=np.int64)
    if verts.ndim != 2 or verts.shape[1] != 3:
        raise ValueError("vertices 必须是 Nx3 数组")
    material_groups: dict[str, MaterialGroup] = {}
    if groups:
        for name, payload in groups.items():
            vis = sorted(set(int(i) for i in payload.get("vertex_indices", [])))
            material_groups[name] = MaterialGroup(name=name, vertex_indices=vis)
    if not material_groups:
        material_groups["default"] = MaterialGroup(
            name="default", vertex_indices=list(range(verts.shape[0]))
        )
    aabb_min = verts.min(axis=0)
    aabb_max = verts.max(axis=0)
    center = (aabb_min + aabb_max) / 2.0
    half_extents = (aabb_max - aabb_min) / 2.0
    normals = _compute_vertex_normals(verts, face_arr) if face_arr.size else np.zeros_like(verts)
    return Model(verts, face_arr, material_groups,
                 aabb_min, aabb_max, center, half_extents, normals)
