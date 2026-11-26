"""Versioned policy commit & incremental invalidation closure (req 6).

Each new policy package gets a new version number and is installed by
inserting the version row and swapping the pointer atomically (never
row-by-row).  Version regressions are rejected.

On a policy change the affected topology domains and subscriber nodes
are computed as the reverse-dependency closure of the changed nodes.
Cached views inside the closure are marked for recompute, views outside
are safely reused, and subscriptions outside the closure are not
re-triggered.
"""

from __future__ import annotations

from typing import Any, Dict, List, Set

from .models import Subscription
from .persistence import Ledger


def dependency_closure(changed_nodes: List[str], topo_views: Dict[str, Dict[str, Any]]) -> Set[str]:
    """Reverse-dependency closure: changed nodes plus every node that
    transitively depends on them."""
    dependents: Dict[str, Set[str]] = {}
    for node, view in topo_views.items():
        for dep in view["depends_on"]:
            dependents.setdefault(dep, set()).add(node)
    closure: Set[str] = set(changed_nodes)
    frontier = list(changed_nodes)
    while frontier:
        cur = frontier.pop()
        for nxt in dependents.get(cur, ()):
            if nxt not in closure:
                closure.add(nxt)
                frontier.append(nxt)
    return closure


def commit_policy(
    ledger: Ledger,
    version: int,
    snapshot: Dict[str, Any],
    changed_nodes: List[str],
    topo_views: Dict[str, Dict[str, Any]],
    subscriptions: List[Subscription],
    clock: int,
) -> Dict[str, Any]:
    ledger.install_policy(version, snapshot, clock)
    closure = dependency_closure(changed_nodes, topo_views)
    affected_domains = sorted({topo_views[n]["scope"] for n in closure if n in topo_views})

    recompute = sorted(n for n in closure if n in topo_views)
    reuse = sorted(n for n in topo_views if n not in closure)

    triggered, skipped = [], []
    for sub in subscriptions:
        if sub.subscriber in closure:
            if ledger.record_callback(sub.service, sub.subscriber, version, clock):
                triggered.append({"service": sub.service, "subscriber": sub.subscriber})
        else:
            skipped.append({"service": sub.service, "subscriber": sub.subscriber})

    return {
        "version": version,
        "changed_nodes": sorted(changed_nodes),
        "closure": sorted(closure),
        "affected_domains": affected_domains,
        "cache_views": {"recompute": recompute, "reuse": reuse},
        "callbacks": {
            "triggered": sorted(triggered, key=lambda c: (c["service"], c["subscriber"])),
            "skipped": sorted(skipped, key=lambda c: (c["service"], c["subscriber"])),
        },
    }
