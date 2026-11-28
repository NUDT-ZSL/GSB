"""Isosurface extraction on a regular grid (marching tetrahedra).

The grid SDF is sampled at the configured resolution, then each cube is
split into the 6 Freudenthal tetrahedra (consistent across the grid, so
the output is watertight and manifold by construction).  Crossing
vertices are deduplicated globally via their grid-edge key, and finally
projected onto the analytic SDF isolevel with a few Newton steps so
mesh vertices land on the zero set of (sdf - iso_threshold).
"""

import numpy as np

from .sdf import eval_tree, sdf_gradient, check_csg_domains
from .errors import WorkbenchError, E_CSG_EMPTY_RESULT

# Freudenthal triangulation of the unit cube; corner id bit layout
# (x, y, z) -> id = x + 2*y + 4*z with the offsets below.
_CORNER_OFFSETS = np.array([
    [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
    [0, 0, 1], [1, 0, 1], [0, 1, 1], [1, 1, 1],
], dtype=np.int64)

_TETS = np.array([
    [0, 1, 3, 7],
    [0, 1, 5, 7],
    [0, 2, 3, 7],
    [0, 2, 6, 7],
    [0, 4, 5, 7],
    [0, 4, 6, 7],
], dtype=np.int64)

_TET_EDGES = np.array([
    [0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3],
], dtype=np.int64)


def sample_grid(asset):
    """Sample the boolean-tree SDF on the asset's regular grid."""
    c = asset["constraints"]
    res = int(c["resolution"])
    iso = float(c["iso_threshold"])
    bmin = np.asarray(asset["bounds"]["min"], float)
    bmax = np.asarray(asset["bounds"]["max"], float)
    axes = [np.linspace(bmin[i], bmax[i], res + 1) for i in range(3)]
    gx, gy, gz = np.meshgrid(*axes, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    # CSG domain checks run on the sampling grid before extraction so a
    # domain mismatch never produces a half-built mesh.
    check_csg_domains(asset["tree"], pts, iso, asset=asset["name"])
    values = eval_tree(asset["tree"], pts, asset=asset["name"])
    grid = values.reshape((res + 1,) * 3)
    if not (np.any(grid < iso) and np.any(grid > iso)):
        raise WorkbenchError(
            E_CSG_EMPTY_RESULT,
            "boolean tree produces no isosurface on the sampling domain",
            asset=asset["name"], node_path="root",
        )
    return grid, axes


def _grid_ids(res):
    n = res + 1
    i, j, k = np.meshgrid(np.arange(n), np.arange(n), np.arange(n),
                          indexing="ij")
    return (i + n * (j + n * k)).astype(np.int64)


def extract(asset):
    """Extract the isosurface; returns (vertices, faces, stats)."""
    c = asset["constraints"]
    res = int(c["resolution"])
    iso = float(c["iso_threshold"])
    grid, axes = sample_grid(asset)
    n = res + 1
    ids = _grid_ids(res)

    cv = np.empty((res, res, res, 8))
    ci = np.empty((res, res, res, 8), dtype=np.int64)
    for corner, off in enumerate(_CORNER_OFFSETS):
        sl = tuple(slice(off[d], off[d] + res) for d in range(3))
        cv[..., corner] = grid[sl]
        ci[..., corner] = ids[sl]

    coords = np.stack(np.meshgrid(*axes, indexing="ij"), axis=-1)

    edge_points = []
    edge_keys = []
    tet_tris = []

    for tet in _TETS:
        v = cv[..., tet]
        g = ci[..., tet]
        inside = v < iso
        mask = inside[..., 0].astype(np.int32)
        for cidx in range(1, 4):
            mask |= inside[..., cidx].astype(np.int32) << cidx
        active = (mask > 0) & (mask < 15)
        if not np.any(active):
            continue
        m = mask[active]
        vv = v[active]
        gg = g[active]
        cross_pos = np.full((m.shape[0], 6, 3), np.nan)
        cross_ok = np.zeros((m.shape[0], 6), dtype=bool)
        key_lo = np.zeros((m.shape[0], 6), dtype=np.int64)
        key_hi = np.zeros((m.shape[0], 6), dtype=np.int64)
        idx = np.argwhere(active)
        for e, (a, b) in enumerate(_TET_EDGES):
            va, vb = vv[:, a], vv[:, b]
            cross = (va < iso) != (vb < iso)
            if not np.any(cross):
                continue
            t = (iso - va[cross]) / (vb[cross] - va[cross])
            ca = idx[cross] + _CORNER_OFFSETS[tet[a]]
            cb = idx[cross] + _CORNER_OFFSETS[tet[b]]
            pa = coords[ca[:, 0], ca[:, 1], ca[:, 2]]
            pb = coords[cb[:, 0], cb[:, 1], cb[:, 2]]
            cross_pos[cross, e] = pa + t[:, None] * (pb - pa)
            cross_ok[cross, e] = True
            ga, gb = gg[cross, a], gg[cross, b]
            key_lo[cross, e] = np.minimum(ga, gb)
            key_hi[cross, e] = np.maximum(ga, gb)

        edge_points.append(cross_pos.reshape(-1, 3))
        edge_keys.append(np.stack([key_lo.ravel(), key_hi.ravel()], axis=1))
        tet_tris.append((m, cross_ok))

    if not edge_points:
        raise WorkbenchError(
            E_CSG_EMPTY_RESULT,
            "boolean tree produces no isosurface on the sampling domain",
            asset=asset["name"], node_path="root",
        )

    all_pos = np.concatenate(edge_points, axis=0)
    all_keys = np.concatenate(edge_keys, axis=0)
    valid = ~np.isnan(all_pos[:, 0])
    all_pos = all_pos[valid]
    all_keys = all_keys[valid]
    key1d = all_keys[:, 0] * (n ** 3 + 1) + all_keys[:, 1]
    uniq, first, inverse = np.unique(key1d, return_index=True,
                                     return_inverse=True)
    vertices = all_pos[first]
    vert_edges = all_keys[first]  # grid-edge endpoint ids per vertex

    # scatter vertex indices back to the full slot array so each tet can
    # slice out its own (M, 6) crossing -> vertex lookup.
    slot_vertex = np.full(valid.shape[0], -1, dtype=np.int64)
    slot_vertex[valid] = inverse

    faces = []
    offset = 0
    for m, cross_ok in tet_tris:
        count = m.shape[0] * 6
        inv = slot_vertex[offset:offset + count].reshape(m.shape[0], 6)
        offset += count
        for corner in range(4):
            bit = 1 << corner
            one_in = (m == bit)
            one_out = (m == (15 & ~bit))
            for sel, flip in ((one_in, False), (one_out, True)):
                if not np.any(sel):
                    continue
                edges = [e for e, (a, b) in enumerate(_TET_EDGES)
                         if a == corner or b == corner]
                tri = inv[sel][:, edges]
                if flip:
                    tri = tri[:, ::-1]
                faces.append(tri)
        for pair in ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)):
            bits = (1 << pair[0]) | (1 << pair[1])
            for target, flip in ((bits, False),):
                sel = m == target
                if not np.any(sel):
                    continue
                a0, a1 = pair
                b0, b1 = [cc for cc in range(4) if cc not in pair]

                def _edge(x, y):
                    for ei, (ea, eb) in enumerate(_TET_EDGES):
                        if {ea, eb} == {x, y}:
                            return ei
                    raise AssertionError

                q = [_edge(a0, b0), _edge(a0, b1),
                     _edge(a1, b1), _edge(a1, b0)]
                quad = inv[sel][:, q]
                t1 = quad[:, [0, 1, 2]]
                t2 = quad[:, [0, 2, 3]]
                if flip:
                    t1 = t1[:, ::-1]
                    t2 = t2[:, ::-1]
                faces.extend([t1, t2])

    faces = np.concatenate(faces, axis=0).astype(np.int64)

    # weld near-coincident vertices: grid vertices whose SDF value is
    # almost exactly the isolevel produce clusters of sliver triangles;
    # welding the cluster (a short-edge collapse) keeps the mesh closed.
    voxel = min(float(axes[d][1] - axes[d][0]) for d in range(3))
    vertices, faces, kept = weld_vertices(vertices, faces, voxel * 1e-4)
    vert_edges = vert_edges[kept]

    # orient consistently: face normal must agree with the SDF gradient
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    normals = np.cross(v1 - v0, v2 - v0)
    centroids = (v0 + v1 + v2) / 3.0
    grad = sdf_gradient(asset["tree"], centroids, asset=asset["name"])
    flip = np.einsum("ij,ij->i", normals, grad) < 0
    faces[flip] = faces[flip][:, [0, 2, 1]]

    # refine each vertex along its own grid edge by bisection: the
    # vertex never leaves the edge segment, so the valid simplicial
    # complex from marching tetrahedra cannot fold or self-intersect.
    vertices = refine_vertices(asset, vertices, iso, vert_edges, coords)
    residual = np.abs(eval_tree(asset["tree"], vertices,
                                asset=asset["name"]) - iso)
    stats = {
        "vertex_count": int(len(vertices)),
        "face_count": int(len(faces)),
        "max_abs_sdf_residual": float(np.max(residual)),
        "mean_abs_sdf_residual": float(np.mean(residual)),
        "iso_threshold": iso,
        "resolution": res,
    }
    return vertices, faces, stats


def weld_vertices(vertices, faces, eps):
    """Merge vertices closer than eps (short-edge collapse).

    Union-find over a 27-neighbourhood spatial hash; faces that become
    degenerate are dropped, which preserves closedness because each
    welded edge removes exactly its incident triangles.
    """
    if len(vertices) == 0:
        return vertices, faces, np.arange(0)
    parent = np.arange(len(vertices))

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    buckets = {}
    keys = np.floor(vertices / eps).astype(np.int64)
    for idx, key in enumerate(keys):
        buckets.setdefault(tuple(key), []).append(idx)
    for idx, key in enumerate(keys):
        kx, ky, kz = key
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for j in buckets.get((kx + dx, ky + dy, kz + dz), ()):
                        if j <= idx:
                            continue
                        if np.linalg.norm(vertices[idx] - vertices[j]) < eps:
                            ri, rj = find(idx), find(j)
                            if ri != rj:
                                parent[rj] = ri
    roots = np.array([find(i) for i in range(len(vertices))])
    uniq, inverse = np.unique(roots, return_inverse=True)
    out_v = vertices[uniq]
    out_f = inverse[faces]
    keep = (out_f[:, 0] != out_f[:, 1]) & (out_f[:, 1] != out_f[:, 2]) \
        & (out_f[:, 2] != out_f[:, 0])
    return out_v, out_f[keep], uniq


def refine_vertices(asset, vertices, iso, vert_edges, coords,
                    iterations=30):
    """Bisect each vertex along its originating grid edge to sdf == iso.

    vertices: (N,3) current positions; vert_edges: (N,2) grid endpoint
    ids; coords: (n+1, n+1, n+1, 3) grid node coordinates.  Converges to
    machine precision without any risk of folding the mesh.
    """
    n1 = coords.shape[0]
    lo_id = vert_edges[:, 0]
    hi_id = vert_edges[:, 1]

    def _pos(gid):
        i = gid % n1
        j = (gid // n1) % n1
        k = gid // (n1 * n1)
        return coords[i, j, k]

    pa = _pos(lo_id)
    pb = _pos(hi_id)
    tree = asset["tree"]
    name = asset["name"]
    fa = eval_tree(tree, pa, asset=name) - iso
    # march t in [0, 1] from pa to pb
    t_lo = np.zeros(len(vertices))
    t_hi = np.ones(len(vertices))
    f_lo = fa
    for _ in range(iterations):
        t_mid = 0.5 * (t_lo + t_hi)
        pm = pa + t_mid[:, None] * (pb - pa)
        fm = eval_tree(tree, pm, asset=name) - iso
        same = (fm * f_lo) > 0
        t_lo = np.where(same, t_mid, t_lo)
        t_hi = np.where(same, t_hi, t_mid)
        f_lo = np.where(same, fm, f_lo)
    t = 0.5 * (t_lo + t_hi)
    return pa + t[:, None] * (pb - pa)
