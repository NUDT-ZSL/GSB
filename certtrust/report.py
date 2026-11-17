"""Deterministic JSON report and human-readable path explanations."""

from __future__ import annotations

from . import TOOL_VERSION
from .validation import PARTIAL, TRUSTED, UNTRUSTED, revocation_propagation


def build_report(engine, conclusions):
    effective = engine.store().effective
    verdicts = {TRUSTED: 0, UNTRUSTED: 0, PARTIAL: 0}
    for concl in conclusions:
        verdicts[concl.verdict] += 1
    counters = dict(engine.state["counters"])
    return {
        "tool": "certtrust",
        "tool_version": TOOL_VERSION,
        "anchor": {
            "snapshot_version": effective.snapshot_version,
            "stable_key": effective.stable_key,
            "policy_version": effective.policy_version,
            "observed_at_ms": effective.observed_at_ms,
            "trusted_roots": sorted(effective.trusted_roots),
        },
        "conclusions": [c.to_dict() for c in conclusions],
        "rejects": list(engine.state["rejects"]),
        "invalidation_propagation": revocation_propagation(conclusions),
        "counters": {
            "claims_total": len(conclusions),
            "trusted": verdicts[TRUSTED],
            "untrusted": verdicts[UNTRUSTED],
            "partial": verdicts[PARTIAL],
            "rejected": len(engine.state["rejects"]),
            "observations_consistent": counters["consistent"],
            "observations_overwritten": counters["overwritten"],
            "recomputed": counters["recomputed"],
            "rebuilds": counters["rebuilds"],
        },
    }


def human_readable(report):
    lines = []
    anchor = report["anchor"]
    lines.append(
        f"effective anchor: snapshot v{anchor['snapshot_version']} "
        f"policy {anchor['policy_version']} "
        f"observed_at {anchor['observed_at_ms']} "
        f"key {anchor['stable_key'][:16]}...")
    lines.append(f"trusted roots: {', '.join(anchor['trusted_roots']) or '(none)'}")
    for concl in report["conclusions"]:
        lines.append("")
        lines.append(concl["explanation"])
        for err in concl["errors"]:
            pos = "-" if err["position"] is None else str(err["position"])
            lines.append(f"    [{err['code']}] at position {pos}: {err['message']}")
    if report["invalidation_propagation"]:
        lines.append("")
        lines.append("invalidation propagation:")
        for entry in report["invalidation_propagation"]:
            kinds = ", ".join(entry["kinds"])
            targets = ", ".join(entry["propagates_to"])
            lines.append(f"    {entry['cert_id']} ({kinds}) -> {targets}")
    counters = report["counters"]
    lines.append("")
    lines.append(
        f"summary: {counters['claims_total']} claims "
        f"({counters['trusted']} trusted, {counters['partial']} partial, "
        f"{counters['untrusted']} untrusted), "
        f"{counters['rejected']} rejected inputs, "
        f"consistent observations {counters['observations_consistent']}, "
        f"overwritten {counters['observations_overwritten']}, "
        f"rebuilds {counters['rebuilds']}")
    return "\n".join(lines)
