"""High-level audit orchestration and deterministic report construction."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .comparison import PairVerdict, Tolerance, compare_pair
from .fixedpoint import Grid
from .loader import load_archives
from .models import Archive, QuantizationRecord, QuantizedItem
from .propagation import build_quantized
from .rangecheck import enforce_ranges


def run_audit(paths: list[str | Path], places: int,
              abs_tolerance: str = "0", relative_ppm: int = 0,
              previous_grid_sig: str | None = None) -> dict[str, Any]:
    """Run a complete audit and return a fully JSON-serializable report.

    Parameters
    ----------
    places:
        Fixed-point grid precision (decimal places), supplied at runtime.
    abs_tolerance / relative_ppm:
        Tolerances expressed in real units / parts-per-million; both are
        quantized onto the same grid before any comparison.
    previous_grid_sig:
        When set, any conclusion carried over from that older grid is tagged
        stale.  Within a fresh report the per-grid signature makes mixing
        impossible; this flag documents a detected grid change explicitly.
    """
    grid = Grid.create(places)
    archives = load_archives(paths)
    quantized = build_quantized(archives, grid)
    enforce_ranges(archives, quantized, grid)
    tolerance = Tolerance.create(grid, abs_tolerance, relative_ppm)

    report: dict[str, Any] = {
        "schema_version": 1,
        "grid": {
            "places": grid.places,
            "factor": grid.factor,
            "signature": grid.signature(),
            "stale_previous_signature": previous_grid_sig,
            "grid_changed": previous_grid_sig is not None
            and previous_grid_sig != grid.signature(),
        },
        "tolerance": {
            "abs_steps": tolerance.abs_steps,
            "relative_ppm": tolerance.rel_ppm,
            "boundary_rule": "inclusive: deviation <= effective_tolerance",
        },
        "archives": [_archive_summary(a) for a in
                     sorted(archives, key=lambda x: x.node_id)],
        "quantized_items": [_quantized_dict(quantized[k])
                            for k in sorted(quantized)],
        "keys": {},
    }

    keys = sorted({key for (_node, key) in quantized})
    for key in keys:
        present = sorted((node, qi) for (node, k), qi in quantized.items()
                         if k == key)
        node_names = [node for node, _ in present]
        pairs: list[dict[str, Any]] = []
        verdicts: list[str] = []
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                verdict = compare_pair(present[i][1], present[j][1], tolerance)
                pairs.append(_verdict_dict(verdict))
                verdicts.append(verdict.verdict)
        report["keys"][key] = {
            "unit": present[0][1].unit,
            "nodes": node_names,
            "pair_count": len(pairs),
            "pairs": pairs,
            "key_verdict": _roll_up(verdicts),
        }

    report["summary"] = _summary(report)
    report["canonical_hash"] = canonical_hash(report)
    return report


def _roll_up(verdicts: list[str]) -> str:
    if not verdicts:
        return "single_node"
    if any(v == "deviating" for v in verdicts):
        return "deviating"
    if any(v == "indistinguishable" for v in verdicts):
        return "indistinguishable"
    return "consistent"


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    counts = {"consistent": 0, "deviating": 0,
              "indistinguishable": 0, "single_node": 0}
    dominated = 0
    for entry in report["keys"].values():
        counts[entry["key_verdict"]] += 1
        dominated += sum(1 for p in entry["pairs"]
                         if p["error_bound_dominated"])
    return {
        "key_counts": counts,
        "error_bound_dominated_pairs": dominated,
        "total_keys": len(report["keys"]),
        "total_pairs": sum(e["pair_count"] for e in report["keys"].values()),
    }


def _archive_summary(archive: Archive) -> dict[str, Any]:
    return {
        "node_id": archive.node_id,
        "version": archive.version,
        "item_count": len(archive.items),
        "keys": archive.keys,
    }


def _quantized_dict(qi: QuantizedItem) -> dict[str, Any]:
    return {
        "node_id": qi.node_id,
        "key": qi.key,
        "unit": qi.unit,
        "source": qi.source,
        "qvalue": qi.qvalue,
        "down_error": qi.down_error,
        "up_error": qi.up_error,
        "trace": {label: _trace_dict(rec) for label, rec in sorted(qi.trace.items())
                  if isinstance(rec, QuantizationRecord)},
        "propagation": qi.trace.get("propagation"),
    }


def _trace_dict(rec: QuantizationRecord) -> dict[str, Any]:
    return asdict(rec)


def _verdict_dict(v: PairVerdict) -> dict[str, Any]:
    return {
        "node_a": v.node_a,
        "node_b": v.node_b,
        "value_a": v.value_a,
        "value_b": v.value_b,
        "deviation": v.deviation,
        "abs_tolerance": v.abs_tolerance,
        "rel_tolerance": v.rel_tolerance,
        "effective_tolerance": v.effective_tolerance,
        "triggered_tolerance": v.triggered_tolerance,
        "within_tolerance": v.within_tolerance,
        "uncertainty": v.uncertainty,
        "error_bound_dominated": v.error_bound_dominated,
        "verdict": v.verdict,
    }


def canonical_hash(report: dict[str, Any]) -> str:
    """Stable SHA-256 over the report excluding the hash field itself."""
    import hashlib
    import json
    payload = {k: v for k, v in report.items() if k != "canonical_hash"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
