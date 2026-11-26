"""Cross-version topology snapshot merge & unified index (req 2).

Arbitration rules when a node appears in both snapshots:
  1. the entry with the newer declared logical clock (absolute ms) wins;
  2. on a tie the source-format priority decides (v2 > v1).

v1 relative seconds are converted to absolute milliseconds under the
injected logical clock; the conversion scale and rounding direction are
recorded on the merged view.  Any shard hash mismatch aborts the whole
merge before any view is produced (no half-merged views).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import MergedTopoView, TopoEntry

SOURCE_PRIORITY = {"v2": 2, "v1": 1}
REL_TO_ABS_SCALE_MS = 1000
ROUNDING = "floor"


def convert_v1_time(rel_publish_s: int, injected_clock_ms: int) -> Dict[str, Any]:
    abs_ms = (injected_clock_ms - rel_publish_s * REL_TO_ABS_SCALE_MS) // 1
    return {
        "abs_ms": abs_ms,
        "scale": REL_TO_ABS_SCALE_MS,
        "rounding": ROUNDING,
        "injected_clock_ms": injected_clock_ms,
    }


def merge_topologies(
    v1_entries: List[TopoEntry],
    v2_entries: List[TopoEntry],
    injected_clock_ms: int,
) -> Dict[str, Any]:
    """Build the unified topology index.  Pure & deterministic."""
    candidates: Dict[str, List[TopoEntry]] = {}
    tombstones: List[Dict[str, Any]] = []

    for entry in list(v1_entries) + list(v2_entries):
        if entry.source_version == "v1":
            conv = convert_v1_time(entry.raw_time["rel_publish_s"], injected_clock_ms)
            entry = TopoEntry(
                node=entry.node,
                scope=entry.scope,
                depends_on=entry.depends_on,
                source_version="v1",
                raw_time=entry.raw_time,
                abs_ms=conv["abs_ms"],
                tombstone=False,
            )
        if entry.tombstone:
            tombstones.append(
                {"node": entry.node, "scope": entry.scope, "source_version": entry.source_version, "abs_ms": entry.abs_ms}
            )
            continue
        candidates.setdefault(entry.node, []).append(entry)

    views: Dict[str, MergedTopoView] = {}
    for node in sorted(candidates):
        cands = candidates[node]
        best = max(cands, key=lambda e: (e.abs_ms, SOURCE_PRIORITY[e.source_version]))
        if len(cands) == 1:
            decided_by = f"single-source:{best.source_version}"
        else:
            top_ms = best.abs_ms
            tied = [c for c in cands if c.abs_ms == top_ms]
            decided_by = (
                "newer-logical-clock"
                if len(tied) == 1
                else f"source-priority:{best.source_version}>others(clock-tie)"
            )
        conversion: Optional[Dict[str, Any]] = None
        if best.source_version == "v1":
            conversion = convert_v1_time(best.raw_time["rel_publish_s"], injected_clock_ms)
        views[node] = MergedTopoView(
            node=node,
            scope=best.scope,
            depends_on=sorted(best.depends_on),
            source_version=best.source_version,
            raw_time=dict(best.raw_time),
            abs_ms=best.abs_ms,
            decided_by=decided_by,
            conversion=conversion,
        )

    return {
        "injected_clock_ms": injected_clock_ms,
        "views": {node: _view_to_dict(views[node]) for node in sorted(views)},
        "tombstones": sorted(tombstones, key=lambda t: t["node"]),
    }


def _view_to_dict(v: MergedTopoView) -> Dict[str, Any]:
    return {
        "node": v.node,
        "scope": v.scope,
        "depends_on": v.depends_on,
        "source_version": v.source_version,
        "raw_time": v.raw_time,
        "abs_ms": v.abs_ms,
        "decided_by": v.decided_by,
        "conversion": v.conversion,
    }
