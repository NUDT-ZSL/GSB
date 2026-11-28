"""Mesh quality checks: manifoldness, closedness, degenerate faces,
triangle-triangle intersections and signed volume."""

import numpy as np


def edge_counts(faces):
    """Map each undirected edge to the number of faces using it."""
    counts = {}
    for tri in faces:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (a, b) if a < b else (b, a)
            counts[key] = counts.get(key, 0) + 1
    return counts


def check_manifold(vertices, faces):
    """Boundary edges, non-manifold edges, degenerate faces."""
    counts = edge_counts(faces)
    boundary = [list(e) for e, c in sorted(counts.items()) if c == 1]
    nonmanifold = [list(e) for e, c in sorted(counts.items()) if c > 2]

    degenerate = []
    v = np.asarray(vertices, float)
    f = np.asarray(faces)
    if len(f):
        rep = (f[:, 0] == f[:, 1]) | (f[:, 1] == f[:, 2]) | \
              (f[:, 2] == f[:, 0])
        area2 = np.linalg.norm(
            np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]]),
            axis=1)
        tiny = area2 < 1e-12
        degenerate = sorted(set(np.nonzero(rep | tiny)[0].tolist()))
    return {
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "degenerate_faces": degenerate,
        "is_closed": len(boundary) == 0,
        "is_manifold": len(boundary) == 0 and len(nonmanifold) == 0,
    }


def signed_volume(vertices, faces):
    v = np.asarray(vertices, float)
    f = np.asarray(faces)
    if len(f) == 0:
        return 0.0
    v0 = v[f[:, 0]]
    v1 = v[f[:, 1]]
    v2 = v[f[:, 2]]
    return float(np.sum(np.einsum("ij,ij->i", v0,
                                  np.cross(v1, v2))) / 6.0)


def _tri_tri_intersect(p1, q1, r1, p2, q2, r2):
    """Moller's triangle-triangle intersection test (boolean)."""
    eps = 1e-12
    n2 = np.cross(q2 - p2, r2 - p2)
    d1 = np.array([np.dot(n2, p1 - p2), np.dot(n2, q1 - p2),
                   np.dot(n2, r1 - p2)])
    if np.all(np.abs(d1) < eps):
        return False  # coplanar: ignore (shared faces handled elsewhere)
    if np.all(d1 > 0) or np.all(d1 < 0):
        return False
    n1 = np.cross(q1 - p1, r1 - p1)
    d2 = np.array([np.dot(n1, p2 - p1), np.dot(n1, q2 - p1),
                   np.dot(n1, r2 - p1)])
    if np.all(np.abs(d2) < eps):
        return False
    if np.all(d2 > 0) or np.all(d2 < 0):
        return False
    d = np.cross(n1, n2)
    norm = np.linalg.norm(d)
    if norm < eps:
        return False
    d = d / norm
    axis = int(np.argmax(np.abs(d)))

    def proj(tri):
        return np.array([np.dot(d, tri[0]), np.dot(d, tri[1]),
                         np.dot(d, tri[2])])

    def interval(proj_v, dist):
        # sort so that the vertex with different sign is handled
        order = np.argsort(proj_v)
        pv = proj_v[order]
        dv = dist[order]
        # intersection of triangle with plane: param along projection
        ts = []
        for a, b in ((0, 1), (0, 2), (1, 2)):
            if (dv[a] > 0) != (dv[b] > 0) and abs(dv[b] - dv[a]) > eps:
                t = pv[a] + (pv[b] - pv[a]) * (0 - dv[a]) / (dv[b] - dv[a])
                ts.append(t)
            elif abs(dv[a]) < eps:
                ts.append(pv[a])
        if not ts:
            return None
        return min(ts), max(ts)

    t1 = proj((p1, q1, r1))
    t2 = proj((p2, q2, r2))
    i1 = interval(t1, d1)
    i2 = interval(t2, d2)
    if i1 is None or i2 is None:
        return False
    return not (i1[1] < i2[0] - eps or i2[1] < i1[0] - eps)


def find_triangle_intersections(vertices, faces, max_pairs=200):
    """Self-intersecting triangle pairs (adjacent pairs excluded).

    Uses a uniform spatial hash on triangle AABBs to avoid O(n^2).
    """
    v = np.asarray(vertices, float)
    f = np.asarray(faces)
    if len(f) == 0:
        return []
    tris = v[f]
    lo = tris.min(axis=1)
    hi = tris.max(axis=1)
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    cell = max(np.median(np.maximum(hi - lo, 1e-9).max(axis=1)) * 2.0,
               diag * 1e-6)
    grid = {}
    for i in range(len(f)):
        a = np.floor(lo[i] / cell).astype(np.int64)
        b = np.floor(hi[i] / cell).astype(np.int64)
        for x in range(a[0], b[0] + 1):
            for y in range(a[1], b[1] + 1):
                for z in range(a[2], b[2] + 1):
                    grid.setdefault((x, y, z), []).append(i)
    pairs = set()
    for bucket in grid.values():
        if len(bucket) > 64:
            continue
        for i in range(len(bucket)):
            for j in range(i + 1, len(bucket)):
                a, b = bucket[i], bucket[j]
                pairs.add((a, b) if a < b else (b, a))
    if not pairs:
        return []
    pairs = np.asarray(sorted(pairs), dtype=np.int64)
    fa = f[pairs[:, 0]]
    fb = f[pairs[:, 1]]
    # vectorised adjacency filter: drop pairs sharing a vertex
    shared = np.zeros(len(pairs), dtype=bool)
    for x in range(3):
        for y in range(3):
            shared |= fa[:, x] == fb[:, y]
    # vectorised AABB overlap filter
    lo_a, hi_a = lo[pairs[:, 0]], hi[pairs[:, 0]]
    lo_b, hi_b = lo[pairs[:, 1]], hi[pairs[:, 1]]
    overlap = np.all((lo_a <= hi_b) & (lo_b <= hi_a), axis=1)
    cand = pairs[~shared & overlap]
    result = []
    for a, b in cand:
        if _tri_tri_intersect(*tris[a], *tris[b]):
            result.append([int(a), int(b)])
            if len(result) >= max_pairs:
                break
    return result


def full_check(vertices, faces, check_intersections=True):
    man = check_manifold(vertices, faces)
    intersections = (find_triangle_intersections(vertices, faces)
                     if check_intersections else [])
    volume = signed_volume(vertices, faces)
    defects = []
    if man["boundary_edges"]:
        defects.append("boundary_edges")
    if man["nonmanifold_edges"]:
        defects.append("nonmanifold_edges")
    if man["degenerate_faces"]:
        defects.append("degenerate_faces")
    if intersections:
        defects.append("self_intersections")
    printable = not defects
    return {
        "manifold": man,
        "self_intersections": intersections,
        "signed_volume": volume,
        "defects": defects,
        "printable": printable,
    }
