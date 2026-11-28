"""Quadric-error-metric mesh simplification (requirement 5).

Classic Garland-Heckbert edge collapse with the link condition and a
normal-flip guard so a closed input stays closed and fold-free.  The
hot path uses plain Python scalars (numpy scalar indexing dominates
otherwise).  The pipeline validates the result (volume change,
deviation, closedness) and falls back to partial simplification when
the target face count cannot be reached within tolerance.
"""

import heapq

import numpy as np


def _q_add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def _q_eval(q, p):
    x, y, z = p
    # symmetric 4x4 stored as (q00,q01,q02,q03,q11,q12,q13,q22,q23,q33)
    q00, q01, q02, q03, q11, q12, q13, q22, q23, q33 = q
    return (q00 * x * x + 2 * q01 * x * y + 2 * q02 * x * z + 2 * q03 * x
            + q11 * y * y + 2 * q12 * y * z + 2 * q13 * y
            + q22 * z * z + 2 * q23 * z + q33)


def _q_optimal(q, pa, pb):
    q00, q01, q02, q03, q11, q12, q13, q22, q23, q33 = q
    # solve A x = -b with A = Q[:3,:3], b = Q[:3,3]
    det = (q00 * (q11 * q22 - q12 * q12)
           - q01 * (q01 * q22 - q12 * q02)
           + q02 * (q01 * q12 - q11 * q02))
    best = None
    if abs(det) > 1e-12:
        bx, by, bz = -q03, -q13, -q23
        dx = (bx * (q11 * q22 - q12 * q12)
              - q01 * (by * q22 - q12 * bz)
              + q02 * (by * q12 - q11 * bz)) / det
        dy = (q00 * (by * q22 - q12 * bz)
              - bx * (q01 * q22 - q12 * q02)
              + q02 * (q01 * bz - by * q02)) / det
        dz = (q00 * (q11 * bz - by * q12)
              - q01 * (q01 * bz - by * q02)
              + bx * (q01 * q12 - q11 * q02)) / det
        cand = (dx, dy, dz)
        if all(abs(c) < 1e6 for c in cand):
            best = cand
    best_cost = _q_eval(q, best) if best else float("inf")
    for cand in (pa, pb, ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2,
                          (pa[2] + pb[2]) / 2)):
        c = _q_eval(q, cand)
        if c < best_cost:
            best_cost = c
            best = cand
    return best, best_cost


def _face_quadric(p0, p1, p2):
    ux, uy, uz = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    vx, vy, vz = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    norm = (nx * nx + ny * ny + nz * nz) ** 0.5
    if norm < 1e-20:
        return (0.0,) * 10
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    d = -(nx * p0[0] + ny * p0[1] + nz * p0[2])
    area = norm / 2.0
    return (nx * nx * area, nx * ny * area, nx * nz * area, nx * d * area,
            ny * ny * area, ny * nz * area, ny * d * area,
            nz * nz * area, nz * d * area, d * d * area)


def simplify(vertices, faces, target_faces, max_move=None, max_edge=None):
    """Collapse edges until at most target_faces remain.

    max_move clamps how far a collapsed vertex may land from both edge
    endpoints, and max_edge caps the edge length of any triangle a
    collapse would create; together they stop long skinny triangles
    from spanning creases and self-intersecting.  Returns numpy arrays.
    """
    pos = [tuple(map(float, p)) for p in np.asarray(vertices, float)]
    tri = [list(map(int, t)) for t in np.asarray(faces)]
    n_v = len(pos)
    quad = [(0.0,) * 10] * n_v
    vfaces = [set() for _ in range(n_v)]
    neighbors = [set() for _ in range(n_v)]
    for fi, (a, b, c) in enumerate(tri):
        q = _face_quadric(pos[a], pos[b], pos[c])
        for w in (a, b, c):
            quad[w] = _q_add(quad[w], q)
            vfaces[w].add(fi)
        neighbors[a].update((b, c))
        neighbors[b].update((a, c))
        neighbors[c].update((a, b))
    active_v = [True] * n_v
    active_f = [True] * len(tri)
    version = [0] * n_v

    heap = []

    def push_edge(u, v):
        q = _q_add(quad[u], quad[v])
        p, cost = _q_optimal(q, pos[u], pos[v])
        if max_move is not None:
            du = sum((p[i] - pos[u][i]) ** 2 for i in range(3)) ** 0.5
            dv = sum((p[i] - pos[v][i]) ** 2 for i in range(3)) ** 0.5
            if du > max_move and dv > max_move:
                p = pos[u] if du < dv else pos[v]
                cost = _q_eval(q, p)
        heapq.heappush(heap, (cost, version[u], version[v],
                              (u, v) if u < v else (v, u), p))

    edges = set()
    for a, b, c in tri:
        edges.add((a, b) if a < b else (b, a))
        edges.add((b, c) if b < c else (c, b))
        edges.add((c, a) if c < a else (a, c))
    for u, v in edges:
        push_edge(u, v)

    n_faces = len(tri)

    def link_ok(u, v):
        thirds = set()
        for fi in vfaces[u] & vfaces[v]:
            if not active_f[fi]:
                continue
            for w in tri[fi]:
                if w != u and w != v:
                    thirds.add(w)
        return (neighbors[u] & neighbors[v]) == thirds

    def creates_fold(u, v, p):
        for fi in vfaces[u] | vfaces[v]:
            if not active_f[fi]:
                continue
            a, b, c = tri[fi]
            if u in (a, b, c) and v in (a, b, c):
                continue  # removed by the collapse
            pts = [pos[a], pos[b], pos[c]]
            if max_edge is not None:
                np_pts = [p if w in (u, v) else pts[k]
                          for k, w in enumerate((a, b, c))]
                too_long = False
                for x in range(3):
                    for y in range(x + 1, 3):
                        d2 = sum((np_pts[x][i] - np_pts[y][i]) ** 2
                                 for i in range(3))
                        if d2 > max_edge * max_edge:
                            too_long = True
                if too_long:
                    return True
            old_n = None
            new_pts = [p if w in (u, v) else pts[k]
                       for k, w in enumerate((a, b, c))]
            for k in (0, 1, 2):
                pass
            o = [pts[1][i] - pts[0][i] for i in range(3)]
            o2 = [pts[2][i] - pts[0][i] for i in range(3)]
            on = (o[1] * o2[2] - o[2] * o2[1],
                  o[2] * o2[0] - o[0] * o2[2],
                  o[0] * o2[1] - o[1] * o2[0])
            e1 = [new_pts[1][i] - new_pts[0][i] for i in range(3)]
            e2 = [new_pts[2][i] - new_pts[0][i] for i in range(3)]
            nn = (e1[1] * e2[2] - e1[2] * e2[1],
                  e1[2] * e2[0] - e1[0] * e2[2],
                  e1[0] * e2[1] - e1[1] * e2[0])
            if on[0] * nn[0] + on[1] * nn[1] + on[2] * nn[2] <= 1e-18:
                return True
        return False

    while n_faces > target_faces and heap:
        cost, vu, vv, (u, v), p = heapq.heappop(heap)
        if not (active_v[u] and active_v[v]):
            continue
        if vu != version[u] or vv != version[v]:
            continue
        if v not in neighbors[u]:
            continue
        if not link_ok(u, v):
            continue
        if creates_fold(u, v, p):
            continue
        pos[u] = p
        quad[u] = _q_add(quad[u], quad[v])
        version[u] += 1
        active_v[v] = False
        for fi in list(vfaces[v]):
            if not active_f[fi]:
                continue
            face = tri[fi]
            for k in (0, 1, 2):
                if face[k] == v:
                    face[k] = u
            if len(set(face)) < 3:
                active_f[fi] = False
                n_faces -= 1
                for w in face:
                    vfaces[w].discard(fi)
            else:
                vfaces[u].add(fi)
        for w in list(neighbors[v]):
            neighbors[w].discard(v)
            if w != u:
                neighbors[w].add(u)
                neighbors[u].add(w)
        neighbors[u].discard(v)
        neighbors[u].discard(u)
        neighbors[v].clear()
        vfaces[v].clear()
        for w in list(neighbors[u]):
            push_edge(u, w)

    keep_v = [i for i in range(n_v) if active_v[i]]
    remap = {old: new for new, old in enumerate(keep_v)}
    out_v = np.array([pos[i] for i in keep_v], dtype=float)
    out_f = np.array([[remap[w] for w in tri[fi]]
                      for fi in range(len(tri)) if active_f[fi]],
                     dtype=np.int64)
    return out_v, out_f
