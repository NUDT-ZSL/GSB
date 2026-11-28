"""Deterministic board-game asset project generator.

Produces, from a fixed seed, the SDF primitive boolean trees, board layout
and constraint parameters for every asset (castle, tree, crystal cluster,
mountain, river, cave) plus boundary-sample assets used to verify the
error-code paths.  No external models or print software are involved.
"""

import copy
import json
import os

SEED = 42


def _prim(ptype, params, translate=(0, 0, 0), rotate=(0, 0, 0),
          scale=(1, 1, 1)):
    return {
        "op": "primitive",
        "primitive": {"type": ptype, "params": params},
        "transform": {
            "translate": list(translate),
            "rotate": list(rotate),
            "scale": list(scale),
        },
    }


def _op(op, *children):
    return {"op": op, "children": list(children)}


def _constraints(resolution, target_faces, min_wall, base_tol=0.05,
                 iso=0.02, vol_tol=0.05, dev_tol=0.12):
    return {
        "resolution": resolution,
        "iso_threshold": iso,
        "target_faces": target_faces,
        "base_tolerance": base_tol,
        "min_wall_thickness": min_wall,
        "volume_tolerance": vol_tol,
        "deviation_tolerance": dev_tol,
    }


def _castle():
    base = _prim("box", {"half_extents": [1.4, 0.9, 1.4]},
                 translate=(0, 0.9, 0))
    towers = []
    for sx in (-1.15, 1.15):
        for sz in (-1.15, 1.15):
            towers.append(_prim(
                "cylinder", {"radius": 0.42, "half_height": 1.3},
                translate=(sx, 1.3, sz)))
            towers.append(_prim(
                "cone", {"radius": 0.55, "height": 0.7},
                translate=(sx, 2.6, sz)))
    keep = _prim("box", {"half_extents": [0.7, 1.2, 0.7]},
                 translate=(0, 1.2, 0))
    door = _prim("box", {"half_extents": [0.3, 0.5, 0.3]},
                 translate=(0, 0.7, 1.35))
    body = _op("union", base, keep, *towers)
    return _op("subtract", body, door)


def _tree():
    trunk = _prim("capsule", {"radius": 0.22, "half_height": 0.8},
                  translate=(0, 0.9, 0))
    canopy = _op(
        "union",
        _prim("sphere", {"radius": 0.9}, translate=(0, 2.2, 0)),
        _prim("sphere", {"radius": 0.6}, translate=(0.55, 1.8, 0.3)),
        _prim("sphere", {"radius": 0.55}, translate=(-0.5, 1.9, -0.35)),
    )
    return _op("union", trunk, canopy)


def _crystal_cluster():
    shards = [
        _prim("pyramid", {"base_half": 0.45, "height": 2.2},
              translate=(0, 0, 0)),
        _prim("pyramid", {"base_half": 0.32, "height": 1.5},
              translate=(0.5, 0, 0.25), rotate=(0, 0, -18)),
        _prim("pyramid", {"base_half": 0.28, "height": 1.2},
              translate=(-0.45, 0, 0.3), rotate=(12, 30, 14)),
        _prim("pyramid", {"base_half": 0.24, "height": 1.0},
              translate=(0.1, 0, -0.5), rotate=(-14, 0, 8)),
    ]
    rock = _prim("box", {"half_extents": [0.9, 0.15, 0.8]},
                 translate=(0, 0.12, 0))
    return _op("union", rock, *shards)


def _mountain():
    body = _op(
        "union",
        _prim("cone", {"radius": 1.6, "height": 2.4}),
        _prim("sphere", {"radius": 0.8}, translate=(0.9, 0.35, 0.5)),
        _prim("sphere", {"radius": 0.7}, translate=(-0.8, 0.3, -0.6)),
    )
    flat = _prim("box", {"half_extents": [3.0, 1.0, 3.0]},
                 translate=(0, 1.0, 0))
    return _op("intersect", body, flat)


def _river():
    segs = []
    pts = [(-1.6, 0.0), (-0.8, 0.5), (0.1, -0.2), (0.9, 0.4), (1.6, 0.0)]
    for x, z in pts:
        segs.append(_prim("capsule", {"radius": 0.45, "half_height": 0.4},
                          translate=(x, 0.12, z), rotate=(90, 0, 0)))
    channel = _op("union", *segs)
    slab = _prim("box", {"half_extents": [2.2, 0.12, 1.2]},
                 translate=(0, 0.12, 0))
    return _op("intersect", channel, slab)


def _cave():
    hill = _prim("sphere", {"radius": 1.5}, translate=(0, 0.4, 0),
                 scale=(1.2, 0.9, 1.1))
    mouth = _prim("sphere", {"radius": 0.55}, translate=(0, 0.35, 1.35))
    chimney = _prim("cylinder", {"radius": 0.25, "half_height": 0.8},
                    translate=(0.4, 1.6, 0))
    return _op("subtract", hill, mouth, chimney)


def _boundary_empty_intersection():
    a = _prim("sphere", {"radius": 0.5}, translate=(-1.5, 0.5, 0))
    b = _prim("sphere", {"radius": 0.5}, translate=(1.5, 0.5, 0))
    return _op("intersect", a, b)


def _boundary_empty_subtrahend():
    a = _prim("sphere", {"radius": 1.0}, translate=(0, 1.0, 0))
    b = _prim("sphere", {"radius": 0.4}, translate=(5.0, 5.0, 5.0))
    return _op("subtract", a, b)


def generate_project(seed=SEED):
    """Build the full deterministic asset project dict."""
    if seed != SEED:
        raise ValueError("only the fixed seed 42 is supported")
    assets = [
        {
            "name": "castle",
            "tree": _castle(),
            "bounds": {"min": [-2.2, -0.2, -2.2], "max": [2.2, 3.6, 2.2]},
            "constraints": _constraints(40, 4000, 0.18),
        },
        {
            "name": "tree",
            "tree": _tree(),
            "bounds": {"min": [-1.8, -0.2, -1.8], "max": [1.8, 3.4, 1.8]},
            "constraints": _constraints(36, 3000, 0.2),
        },
        {
            "name": "crystal_cluster",
            "tree": _crystal_cluster(),
            "bounds": {"min": [-1.4, -0.6, -1.4], "max": [1.4, 2.6, 1.4]},
            "constraints": _constraints(48, 5000, 0.1, base_tol=0.04),
        },
        {
            "name": "mountain",
            "tree": _mountain(),
            "bounds": {"min": [-2.0, -0.2, -2.0], "max": [2.0, 2.8, 2.0]},
            "constraints": _constraints(40, 3500, 0.25),
        },
        {
            "name": "river",
            "tree": _river(),
            "bounds": {"min": [-2.4, -0.1, -1.4], "max": [2.4, 0.5, 1.4]},
            "constraints": _constraints(40, 2500, 0.1, base_tol=0.03),
        },
        {
            "name": "cave",
            "tree": _cave(),
            "bounds": {"min": [-2.2, -1.3, -2.2], "max": [2.2, 2.4, 2.2]},
            "constraints": _constraints(40, 4000, 0.3),
        },
        # Boundary samples: exercise the CSG domain error paths.  They are
        # excluded from normal extraction runs.
        {
            "name": "sample_empty_intersection",
            "boundary_sample": True,
            "tree": _boundary_empty_intersection(),
            "bounds": {"min": [-2.5, -0.5, -1.0], "max": [2.5, 1.5, 1.0]},
            "constraints": _constraints(24, 1000, 0.1),
        },
        {
            "name": "sample_empty_subtrahend",
            "boundary_sample": True,
            "tree": _boundary_empty_subtrahend(),
            "bounds": {"min": [-1.5, -0.5, -1.5], "max": [1.5, 2.5, 1.5]},
            "constraints": _constraints(24, 1000, 0.1),
        },
    ]
    layout = [
        {"asset": "castle", "position": [0.0, 0.0, 0.0], "yaw_deg": 0.0,
         "scale": 1.0},
        {"asset": "tree", "position": [6.0, 0.0, 2.0], "yaw_deg": 35.0,
         "scale": 1.0},
        {"asset": "crystal_cluster", "position": [-5.0, 0.0, 3.0],
         "yaw_deg": -20.0, "scale": 1.0},
        {"asset": "mountain", "position": [5.5, 0.0, -4.5], "yaw_deg": 110.0,
         "scale": 1.2},
        {"asset": "river", "position": [-4.0, 0.0, -3.5], "yaw_deg": 65.0,
         "scale": 1.0},
        {"asset": "cave", "position": [0.5, 0.0, 6.5], "yaw_deg": 180.0,
         "scale": 1.0},
    ]
    return {"seed": seed, "assets": assets, "board_layout": layout}


def asset_summary(asset):
    from .sdf import count_primitives
    return {
        "name": asset["name"],
        "boundary_sample": bool(asset.get("boundary_sample")),
        "primitive_count": count_primitives(asset["tree"]),
        "constraints": asset["constraints"],
        "bounds": asset["bounds"],
    }


def board_transform(placement):
    """Local asset coords -> board coords (yaw about +Y, then translate)."""
    import numpy as np
    from .sdf import euler_to_matrix
    rot = euler_to_matrix(0.0, placement.get("yaw_deg", 0.0), 0.0)
    s = float(placement.get("scale", 1.0))
    t = np.asarray(placement.get("position", [0, 0, 0]), float)

    def apply(pts):
        return (np.asarray(pts, float) @ rot.T) * s + t

    return apply


def save_project(project, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(project, fh, indent=2)


def load_project(path):
    if not os.path.exists(path):
        project = generate_project()
        save_project(project, path)
        return project
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def get_asset(project, name):
    from .errors import WorkbenchError, E_ASSET_NOT_FOUND
    for asset in project["assets"]:
        if asset["name"] == name:
            return asset
    raise WorkbenchError(E_ASSET_NOT_FOUND, f"no asset named {name!r}",
                         asset=name)


def clone_project(project):
    return copy.deepcopy(project)
