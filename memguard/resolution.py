"""Declaration resolution: generation arbitration, tombstones, cycles,
and affected-set computation for declaration evolution."""

from __future__ import annotations

from . import codes
from .model import Reject


def arbitrate(decls):
    """Pick the authoritative declaration per region_id.

    Highest generation wins; on a generation tie a tombstone overrides a
    non-tombstone declaration. Returns winners mapping region_id -> decl.
    """
    winners = {}
    for decl in decls:
        cur = winners.get(decl.region_id)
        if cur is None:
            winners[decl.region_id] = decl
            continue
        if (decl.generation, decl.tombstone) > (cur.generation, cur.tombstone):
            winners[decl.region_id] = decl
    return winners


def resolve(decls):
    """Resolve declarations into a live region table.

    Returns (regions, rejects): regions maps region_id -> RegionDecl for
    non-tombstoned regions with resolvable, acyclic parent chains.
    """
    winners = arbitrate(decls)
    rejects = []
    live = {rid: d for rid, d in winners.items() if not d.tombstone}

    bad_parents = set()
    for rid in sorted(live):
        decl = live[rid]
        if decl.parent_id is not None and decl.parent_id not in live:
            bad_parents.add(rid)
            rejects.append(Reject(decl.file_id, decl.line_no, codes.E_PARENT_UNKNOWN,
                                  f"region {rid!r} parent {decl.parent_id!r} not resolvable"))

    # Precise cycle extraction: nodes that can reach themselves via parents.
    cycle_members = set()
    for rid in live:
        seen = set()
        node = rid
        while node is not None and node in live and node not in seen:
            seen.add(node)
            node = live[node].parent_id
        if node == rid:
            cycle_members |= seen
    if cycle_members:
        rejects.append(Reject("", 0, codes.E_REGION_CYCLE,
                              "cycle regions: " + ",".join(sorted(cycle_members))))

    drop = bad_parents | cycle_members
    regions = {rid: d for rid, d in live.items() if rid not in drop}
    return regions, rejects


def descendants_closure(region_ids, regions):
    """All regions transitively parented under any of region_ids."""
    children = {}
    for rid, decl in regions.items():
        if decl.parent_id is not None:
            children.setdefault(decl.parent_id, set()).add(rid)
    out = set()
    stack = list(region_ids)
    while stack:
        node = stack.pop()
        for child in children.get(node, ()):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def affected_regions(old_regions, new_regions):
    """Regions whose conclusions may change when moving old -> new.

    A region is affected if its declaration appeared, vanished, or changed
    interval/lifecycle/parent, plus the transitive descendant closure in
    both the old and new parent graphs.
    """
    changed = set()
    for rid in set(old_regions) | set(new_regions):
        old, new = old_regions.get(rid), new_regions.get(rid)
        if old is None or new is None:
            changed.add(rid)
            continue
        old_key = (old.start, old.end, old.life_start_ms, old.life_end_ms,
                   old.parent_id, old.arena_size, old.generation)
        new_key = (new.start, new.end, new.life_start_ms, new.life_end_ms,
                   new.parent_id, new.arena_size, new.generation)
        if old_key != new_key:
            changed.add(rid)
    affected = set(changed)
    affected |= descendants_closure(changed, old_regions)
    affected |= descendants_closure(changed, new_regions)
    return affected
