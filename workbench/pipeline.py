"""End-to-end asset pipeline: extract -> check -> flatten -> simplify ->
measure wall thickness, producing a single printable report dict."""

import numpy as np

from . import mesher, meshcheck, flatness, simplify as qem, thickness
from .errors import validate_constraints
from .sdf import eval_tree
from .generator import board_transform


def _placement_for(project, asset_name):
    for p in project.get("board_layout", []):
        if p["asset"] == asset_name:
            return p
    return None


def run_asset(project, asset):
    """Run the full pipeline for one asset.  Raises WorkbenchError on
    invalid parameters or CSG domain mismatches before any mesh exists."""
    validate_constraints(asset)
    c = asset["constraints"]
    iso = float(c["iso_threshold"])
    name = asset["name"]

    vertices, faces, extract_stats = mesher.extract(asset)

    checks = meshcheck.full_check(vertices, faces)
    base = flatness.check_base_flatness(vertices, faces,
                                        float(c["base_tolerance"]))

    # --- simplification with validation + rollback -------------------
    target = int(c["target_faces"])
    vol_tol = float(c["volume_tolerance"])
    dev_tol = float(c["deviation_tolerance"])
    orig_volume = checks["signed_volume"]

    voxel = max(
        (np.asarray(asset["bounds"]["max"], float)
         - np.asarray(asset["bounds"]["min"], float)).max()
        / float(c["resolution"]), 1e-6)

    def validate(sv, sf):
        closed = meshcheck.check_manifold(sv, sf)["is_closed"]
        inter = meshcheck.find_triangle_intersections(sv, sf, max_pairs=1)
        vol = meshcheck.signed_volume(sv, sf)
        vol_err = abs(vol - orig_volume) / max(abs(orig_volume), 1e-12)
        dev = float(np.max(np.abs(
            eval_tree(asset["tree"], sv, asset=name) - iso))) \
            if len(sv) else 0.0
        return {
            "closed": bool(closed),
            "self_intersections": len(inter),
            "volume": vol,
            "volume_error": float(vol_err),
            "max_deviation": dev,
            "ok": bool(closed and not inter and vol_err <= vol_tol
                       and dev <= dev_tol),
        }

    simpl = {
        "target_faces": target,
        "original_faces": int(len(faces)),
        "rolled_back": False,
        "rollback_reason": None,
    }
    if len(faces) <= target:
        simp_v, simp_f = vertices, faces
        simpl["note"] = "face count already at or below target"
        simpl["result_faces"] = int(len(faces))
        simpl["validation"] = validate(simp_v, simp_f)
    else:
        simp_v, simp_f = qem.simplify(vertices, faces, target,
                                      max_move=voxel * 2.0,
                                      max_edge=voxel * 3.0)
        val = validate(simp_v, simp_f)
        if val["ok"]:
            simpl["result_faces"] = int(len(simp_f))
            simpl["validation"] = val
        else:
            # partial simplification fallback: binary search the largest
            # reachable face count that still satisfies the tolerances.
            reasons = []
            if not val["closed"]:
                reasons.append("simplified mesh is not closed")
            if val["self_intersections"]:
                reasons.append("simplified mesh self-intersects")
            if val["volume_error"] > vol_tol:
                reasons.append(
                    f"volume error {val['volume_error']:.4f} exceeds "
                    f"tolerance {vol_tol}")
            if val["max_deviation"] > dev_tol:
                reasons.append(
                    f"max deviation {val['max_deviation']:.4f} exceeds "
                    f"tolerance {dev_tol}")
            lo, hi = target, int(len(faces))
            best = None
            for _ in range(6):
                if hi - lo <= 1:
                    break
                mid = (lo + hi) // 2
                mv, mf = qem.simplify(vertices, faces, mid,
                                      max_move=voxel * 2.0,
                                      max_edge=voxel * 3.0)
                mval = validate(mv, mf)
                if mval["ok"]:
                    best = (mv, mf, mval)
                    hi = mid
                else:
                    lo = mid
            if best is not None:
                simp_v, simp_f, val = best
                simpl["rolled_back"] = True
                simpl["rollback_reason"] = (
                    "full simplification to target failed validation ("
                    + "; ".join(reasons)
                    + "); fell back to partial simplification")
                simpl["result_faces"] = int(len(simp_f))
                simpl["validation"] = val
            else:
                simp_v, simp_f = vertices, faces
                simpl["rolled_back"] = True
                simpl["rollback_reason"] = (
                    "no simplification level satisfies the tolerances ("
                    + "; ".join(reasons)
                    + "); kept the original mesh")
                simpl["result_faces"] = int(len(simp_f))
                simpl["validation"] = validate(simp_v, simp_f)

    # --- wall thickness on the print candidate mesh -------------------
    placement = _placement_for(project, name)
    board_apply = board_transform(placement) if placement else None
    wall = thickness.wall_thickness(
        asset, simp_v, simp_f, float(c["min_wall_thickness"]),
        board_apply=board_apply, iso=iso)

    printable = (checks["printable"] and base["flat"] and wall["passed"]
                 and simpl["validation"]["ok"])

    report = {
        "asset": name,
        "constraints": c,
        "extraction": extract_stats,
        "mesh_check": {
            "defects": checks["defects"],
            "boundary_edge_count": len(checks["manifold"]["boundary_edges"]),
            "nonmanifold_edge_count":
                len(checks["manifold"]["nonmanifold_edges"]),
            "degenerate_face_count":
                len(checks["manifold"]["degenerate_faces"]),
            "self_intersection_pairs": checks["self_intersections"],
            "is_closed": checks["manifold"]["is_closed"],
            "is_manifold": checks["manifold"]["is_manifold"],
            "signed_volume": checks["signed_volume"],
            "printable": checks["printable"],
        },
        "base_flatness": base,
        "simplification": simpl,
        "wall_thickness": wall,
        "placement": placement,
        "printable": bool(printable),
    }
    meshes = {
        "vertices": vertices,
        "faces": faces,
        "simplified_vertices": simp_v,
        "simplified_faces": simp_f,
    }
    return report, meshes
