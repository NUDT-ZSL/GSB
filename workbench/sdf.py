"""Signed-distance-field primitives, transforms and boolean trees.

All evaluators are vectorised: they accept an (N, 3) float array of query
points and return an (N,) array of signed distances (negative inside).

A boolean tree node is a dict:
    {"op": "primitive", "primitive": {...}, "transform": {...}}
    {"op": "union"|"intersect"|"subtract", "children": [node, ...]}

Primitive transforms are local TRS: translate (3,), rotate (Euler XYZ
degrees, 3,), scale (3,).  The world point is mapped into primitive-local
space before evaluating the base SDF.
"""

import numpy as np

from .errors import (
    WorkbenchError,
    E_INVALID_PRIMITIVE,
    E_INVALID_OPERATOR,
    E_CSG_EMPTY_INTERSECTION,
    E_CSG_EMPTY_SUBTRAHEND,
)


def euler_to_matrix(rx, ry, rz):
    """Euler XYZ (degrees) -> 3x3 rotation matrix."""
    rx, ry, rz = np.radians([rx, ry, rz])
    cx, sx = np.cos(rx), np.sin(rx)
    cy, sy = np.cos(ry), np.sin(ry)
    cz, sz = np.cos(rz), np.sin(rz)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]])
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]])
    return Rz @ Ry @ Rx


class Transform:
    """Rigid + scale transform applied to a primitive (local TRS)."""

    def __init__(self, spec=None):
        spec = spec or {}
        self.translate = np.asarray(spec.get("translate", [0, 0, 0]), float)
        rotate = spec.get("rotate", [0, 0, 0])
        self.rotation = euler_to_matrix(*rotate)
        self.scale = np.asarray(spec.get("scale", [1, 1, 1]), float)
        self.scale[self.scale == 0] = 1.0
        self.inv_rotation = self.rotation.T
        self.dist_scale = float(np.min(np.abs(self.scale)))

    def to_local(self, pts):
        p = pts - self.translate
        p = p @ self.inv_rotation.T
        return p / self.scale


def _sdf_sphere(p, r):
    return np.linalg.norm(p, axis=1) - r


def _sdf_box(p, hx, hy, hz):
    q = np.abs(p) - np.array([hx, hy, hz])
    outside = np.linalg.norm(np.maximum(q, 0.0), axis=1)
    inside = np.minimum(np.max(q, axis=1), 0.0)
    return outside + inside


def _sdf_capsule(p, radius, half_height):
    py = p[:, 1] - np.clip(p[:, 1], -half_height, half_height)
    d = np.sqrt(p[:, 0] ** 2 + py ** 2 + p[:, 2] ** 2)
    return d - radius


def _sdf_cylinder(p, radius, half_height):
    d = np.stack([np.sqrt(p[:, 0] ** 2 + p[:, 2] ** 2) - radius,
                  np.abs(p[:, 1]) - half_height], axis=1)
    outside = np.linalg.norm(np.maximum(d, 0.0), axis=1)
    inside = np.minimum(np.max(d, axis=1), 0.0)
    return outside + inside


def _sdf_cone(p, radius, height):
    # solid cone as the intersection of three half-spaces: the lateral
    # surface plane (in the meridian profile), the base plane y=0 and
    # the apex plane y=height.  Continuous everywhere; exact on the
    # boundary of the solid.
    radial = np.sqrt(p[:, 0] ** 2 + p[:, 2] ** 2)
    length = float(np.hypot(height, radius))
    d_side = (height * radial + radius * (p[:, 1] - height)) / length
    d_base = -p[:, 1]
    d_top = p[:, 1] - height
    return np.maximum(np.maximum(d_side, d_base), d_top)


def _sdf_pyramid(p, base_half, height):
    """Square pyramid, base centred at origin on y=0, apex up.

    Built from the sloped face plane plus base/top planes; keeps sharp
    ridges so crystal/pyramid edges do not collapse.
    """
    ax = np.abs(p[:, 0])
    az = np.abs(p[:, 2])
    h = height
    b = base_half
    n = np.array([h, 2.0 * b, h])
    n = n / np.linalg.norm(n)
    d_slope = (ax - b) * n[0] + p[:, 1] * n[1] + (az - b) * n[2]
    d_base = -p[:, 1]
    d_top = p[:, 1] - h
    return np.maximum(np.maximum(d_slope, d_base), d_top)


def _sdf_torus(p, major, minor):
    q = np.stack([np.sqrt(p[:, 0] ** 2 + p[:, 2] ** 2) - major, p[:, 1]],
                 axis=1)
    return np.linalg.norm(q, axis=1) - minor


_PRIMITIVES = {
    "sphere": (_sdf_sphere, {"radius": 1.0}),
    "box": (_sdf_box, {"half_extents": [0.5, 0.5, 0.5]}),
    "capsule": (_sdf_capsule, {"radius": 0.5, "half_height": 0.5}),
    "cylinder": (_sdf_cylinder, {"radius": 0.5, "half_height": 0.5}),
    "cone": (_sdf_cone, {"radius": 0.5, "height": 1.0}),
    "pyramid": (_sdf_pyramid, {"base_half": 0.5, "height": 1.0}),
    "torus": (_sdf_torus, {"major": 0.75, "minor": 0.25}),
}


def eval_primitive(spec, pts):
    ptype = spec.get("type")
    if ptype not in _PRIMITIVES:
        raise WorkbenchError(
            E_INVALID_PRIMITIVE,
            f"unknown primitive type {ptype!r}",
            param="primitive.type",
        )
    fn, defaults = _PRIMITIVES[ptype]
    params = dict(defaults)
    params.update(spec.get("params", {}))
    args = []
    for key in defaults:
        val = params[key]
        if isinstance(val, (list, tuple)):
            args.extend(float(v) for v in val)
        else:
            args.append(float(val))
    return fn(pts, *args)


def count_primitives(node):
    if node.get("op") == "primitive":
        return 1
    return sum(count_primitives(c) for c in node.get("children", []))


def eval_tree(node, pts, asset=None, path="root"):
    """Evaluate the boolean tree at points (N,3) -> (N,)."""
    op = node.get("op")
    if op == "primitive":
        tf = Transform(node.get("transform"))
        local = tf.to_local(pts)
        return eval_primitive(node.get("primitive", {}), local) * tf.dist_scale
    children = node.get("children", [])
    if not children:
        raise WorkbenchError(
            E_INVALID_OPERATOR,
            f"boolean node {path} has no children",
            asset=asset, node_path=path,
        )
    vals = [eval_tree(c, pts, asset=asset, path=f"{path}/{op}[{i}]")
            for i, c in enumerate(children)]
    if op == "union":
        out = vals[0]
        for v in vals[1:]:
            out = np.minimum(out, v)
        return out
    if op == "intersect":
        out = vals[0]
        for v in vals[1:]:
            out = np.maximum(out, v)
        return out
    if op == "subtract":
        out = vals[0]
        for v in vals[1:]:
            out = np.maximum(out, -v)
        return out
    raise WorkbenchError(
        E_INVALID_OPERATOR,
        f"unknown boolean operator {op!r}",
        asset=asset, node_path=path,
    )


def sdf_gradient(node, pts, eps=1e-4, asset=None):
    """Central-difference gradient of the tree SDF at pts."""
    grad = np.zeros_like(pts)
    for axis in range(3):
        delta = np.zeros(3)
        delta[axis] = eps
        hi = eval_tree(node, pts + delta, asset=asset)
        lo = eval_tree(node, pts - delta, asset=asset)
        grad[:, axis] = (hi - lo) / (2.0 * eps)
    return grad


def check_csg_domains(node, pts, iso, asset=None, path="root"):
    """Detect CSG domain mismatches (requirement 8).

    - intersect node whose children never overlap on the sample domain
      -> E_CSG_EMPTY_INTERSECTION
    - subtract node whose subtrahend is empty on the sample domain
      -> E_CSG_EMPTY_SUBTRAHEND
    """
    op = node.get("op")
    if op == "primitive":
        return
    children = node.get("children", [])
    vals = [eval_tree(c, pts, asset=asset, path=f"{path}/{op}[{i}]")
            for i, c in enumerate(children)]
    if op == "intersect":
        inside_any = None
        for v in vals:
            m = v <= iso
            inside_any = m if inside_any is None else (inside_any & m)
        if inside_any is not None and not np.any(inside_any):
            raise WorkbenchError(
                E_CSG_EMPTY_INTERSECTION,
                "intersect operands have no overlapping region on the "
                "sampling domain",
                asset=asset, node_path=path,
            )
    elif op == "subtract":
        for i, v in enumerate(vals[1:], start=1):
            if not np.any(v <= iso):
                raise WorkbenchError(
                    E_CSG_EMPTY_SUBTRAHEND,
                    f"subtract operand {i} is empty on the sampling domain",
                    asset=asset, node_path=f"{path}/subtract[{i}]",
                )
    for i, c in enumerate(children):
        check_csg_domains(c, pts, iso, asset=asset, path=f"{path}/{op}[{i}]")
