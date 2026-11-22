"""Compute-graph topology: the five DAG nodes and their edges."""
from .errors import BusinessError, E_INCONSISTENT

PER_IMAGE_NODES = ("preprocess", "features", "score", "decide")
GLOBAL_NODES = ("metrics",)
ALL_NODES = PER_IMAGE_NODES + GLOBAL_NODES


def _adjacency(edges):
    nodes = set()
    adj = {}
    for a, b in edges:
        nodes.add(a)
        nodes.add(b)
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, [])
    return nodes, adj


def check_acyclic(edges):
    nodes, adj = _adjacency(edges)
    indeg = {n: 0 for n in nodes}
    for a, b in edges:
        indeg[b] += 1
    queue = sorted(n for n in nodes if indeg[n] == 0)
    seen = 0
    while queue:
        n = queue.pop()
        seen += 1
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if seen != len(nodes):
        raise BusinessError(
            E_INCONSISTENT, "compute graph has a cycle",
            edges=[list(e) for e in edges],
        )
    return True


def topo_order(edges):
    check_acyclic(edges)
    nodes, adj = _adjacency(edges)
    indeg = {n: 0 for n in nodes}
    for a, b in edges:
        indeg[b] += 1
    queue = sorted(n for n in nodes if indeg[n] == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in sorted(adj[n]):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
                queue.sort()
    return order


def downstream_of(node, edges):
    """All nodes reachable from `node` (exclusive), i.e. the affected suffix."""
    _, adj = _adjacency(edges)
    out, stack = set(), [node]
    while stack:
        n = stack.pop()
        for m in adj.get(n, []):
            if m not in out:
                out.add(m)
                stack.append(m)
    return out
