"""Local wall-thickness field via inward-normal SDF probing (requirement 6).

From each surface vertex the inward normal ray is walked; the deepest
interior distance along the ray (the peak of -sdf) approximates the
local medial-axis radius, so local wall thickness ~= 2 * peak.  This is
robust against the concave-crease false positives that plague naive
ray-to-mesh distance probes.  Vertices below the minimum wall thickness
are grouped into connected regions and reported in both asset-local and
board coordinates so thin-wall zones map to the real piece size.
"""

import numpy as np

from .flatness import _vertex_adjacency, connected_components
from .sdf import eval_tree


def vertex_normals(vertices, faces):
    v = np.asarray(vertices, float)
    f = np.asarray(faces)
    n = np.zeros_like(v)
    v0, v1, v2 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    fn = np.cross(v1 - v0, v2 - v0)
    for k in range(3):
        np.add.at(n, f[:, k], fn)
    norm = np.linalg.norm(n, axis=1)
    norm[norm < 1e-12] = 1e-12
    return n / norm[:, None]


def _thickness_field(asset, vertices, iso, steps=160):
    """Local wall thickness along the inward SDF-gradient ray.

    Two measurements are combined per vertex:
      - t_exit: distance along the ray until the field climbs back to
        the surface level (the "opposite wall" distance);
      - 2x the peak of -sdf along the ray (the medial-axis estimate).
    The maximum of the two is reported: a genuine thin wall fails both,
    while convex sharp corners (small medial estimate only) and concave
    CSG creases (small exit distance only) are not falsely flagged.
    The analytic gradient (not the mesh normal) sets the ray direction,
    so sliver triangles cannot aim the ray outside the solid.
    """
    from .sdf import sdf_gradient
    diag = float(np.linalg.norm(vertices.max(axis=0) - vertices.min(axis=0)))
    t_max = diag * 0.5
    ts = np.linspace(0.0, t_max, steps)
    grad = sdf_gradient(asset["tree"], vertices, asset=asset["name"])
    norm = np.linalg.norm(grad, axis=1)
    norm[norm < 1e-12] = 1e-12
    dirs = grad / norm[:, None]
    peak = np.full(len(vertices), -iso)
    t_exit = np.full(len(vertices), t_max)
    batch = 512
    for start in range(0, len(vertices), batch):
        v = vertices[start:start + batch]
        n = dirs[start:start + batch]
        # (B, S, 3) points along each ray
        pts = v[:, None, :] - n[:, None, :] * ts[None, :, None]
        vals = eval_tree(asset["tree"], pts.reshape(-1, 3),
                         asset=asset["name"]).reshape(len(v), steps)
        peak[start:start + batch] = np.max(-vals, axis=1)
        # first exit: field returns to the surface level after having
        # dipped clearly inside the solid
        entered = vals < -2 * iso
        exited = vals >= iso
        # mask of exit candidates only valid after entering
        entered_cum = np.cumsum(entered, axis=1) > 0
        exit_mask = exited & entered_cum
        has_exit = exit_mask.any(axis=1)
        first = np.argmax(exit_mask, axis=1)
        t_exit[start:start + batch] = np.where(has_exit, ts[first], t_max)
    medial = np.maximum(2.0 * peak, 0.0)
    return np.maximum(medial, t_exit)


def wall_thickness(asset, vertices, faces, min_wall_thickness,
                   board_apply=None, iso=0.02):
    v = np.asarray(vertices, float)
    thickness = _thickness_field(asset, v, iso)
    thin = thickness < min_wall_thickness
    adj = _vertex_adjacency(faces, len(v))
    comps = connected_components(thin, adj)
    regions = []
    for comp in comps:
        comp = np.asarray(comp)
        centroid = np.mean(v[comp], axis=0)
        region = {
            "vertex_indices": comp.tolist(),
            "vertex_count": int(len(comp)),
            "min_thickness": float(np.min(thickness[comp])),
            "centroid_local": [float(x) for x in centroid],
        }
        if board_apply is not None:
            board_pt = board_apply(centroid[None, :])[0]
            region["centroid_board"] = [float(x) for x in board_pt]
        regions.append(region)
    regions.sort(key=lambda r: r["min_thickness"])
    return {
        "min_wall_thickness": min_wall_thickness,
        "thin_vertex_count": int(np.count_nonzero(thin)),
        "min_thickness_found": float(np.min(thickness))
        if len(thickness) else None,
        "regions": regions,
        "passed": len(regions) == 0,
    }
