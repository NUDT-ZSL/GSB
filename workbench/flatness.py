"""Base flatness diagnostics (requirement 4).

Only downward-facing surface matters for a piece that must stand flat:
the base plane sits at the lowest vertex (y = y_min); downward-facing
vertices inside the tolerance band form the base set, and
downward-facing vertices near the bottom but outside the band are
out-of-tolerance ledges, reported as connected zones.
"""

import numpy as np


def _vertex_adjacency(faces, n_vertices):
    adj = [set() for _ in range(n_vertices)]
    for a, b, c in faces:
        adj[a].update((b, c))
        adj[b].update((a, c))
        adj[c].update((a, b))
    return adj


def connected_components(flagged, adj):
    """Connected components of a boolean vertex mask."""
    flagged = np.asarray(flagged, bool)
    seen = np.zeros_like(flagged)
    comps = []
    for start in np.nonzero(flagged)[0]:
        if seen[start]:
            continue
        stack = [int(start)]
        seen[start] = True
        comp = []
        while stack:
            u = stack.pop()
            comp.append(u)
            for w in adj[u]:
                if flagged[w] and not seen[w]:
                    seen[w] = True
                    stack.append(w)
        comps.append(comp)
    return comps


def _downward_vertex_mask(vertices, faces):
    """Vertices incident to faces whose outward normal points down."""
    v = np.asarray(vertices, float)
    f = np.asarray(faces)
    v0, v1, v2 = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    n = np.cross(v1 - v0, v2 - v0)
    norm = np.linalg.norm(n, axis=1)
    norm[norm < 1e-20] = 1e-20
    ny = n[:, 1] / norm
    down_faces = ny < -0.7
    mask = np.zeros(len(v), dtype=bool)
    mask[np.unique(f[down_faces].ravel())] = True
    return mask


def check_base_flatness(vertices, faces, base_tolerance):
    v = np.asarray(vertices, float)
    y = v[:, 1]
    y_min = float(np.min(y))
    downward = _downward_vertex_mask(vertices, faces)
    band = downward & (y <= y_min + base_tolerance)
    near = downward & (y <= y_min + 3.0 * base_tolerance)
    over = near & ~band

    base_dev = y[band] - y_min
    max_protrusion = float(np.max(base_dev)) if np.any(band) else 0.0

    adj = _vertex_adjacency(faces, len(v))
    over_comps = connected_components(over, adj)
    zones = []
    for comp in over_comps:
        comp = np.asarray(comp)
        zones.append({
            "vertex_indices": comp.tolist(),
            "vertex_count": int(len(comp)),
            "max_deviation": float(np.max(y[comp]) - y_min),
            "centroid": [float(x) for x in np.mean(v[comp], axis=0)],
        })
    zones.sort(key=lambda z: -z["max_deviation"])

    # deepest dent: lowest downward-facing vertex above the band would
    # be a ledge; the depression metric tracks the largest gap between
    # the base plane and such ledges (0 when the base is one plane)
    max_depression = max((z["max_deviation"] for z in zones), default=0.0)

    return {
        "base_plane_y": y_min,
        "base_tolerance": base_tolerance,
        "base_vertex_count": int(np.count_nonzero(band)),
        "max_protrusion": max_protrusion,
        "max_depression": max_depression,
        "out_of_tolerance_zones": zones,
        "flat": len(zones) == 0 and max_protrusion <= base_tolerance,
    }
