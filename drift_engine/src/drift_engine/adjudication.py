"""Disposition-window and freeze-rule adjudication with explicit quantization."""

from __future__ import annotations

from . import codes
from .merge import merge_view
from .parsers import Evidence
from .spec import BaselineSpec


def adjudicate(entry: dict, spec: BaselineSpec, window_seconds: int,
               decision_clock_ms: int, quantum_ms_by_source: dict) -> dict:
    """Adjudicate one merged drift-state entry.

    The window is declared in whole seconds while observations are in integer
    milliseconds. v1 relative-second evidence carries a 1000 ms quantum, so the
    true first-crossing instant lies in a +/- 999 ms band around the stored
    value. v2 millisecond evidence has a zero-width band.
    """
    host, key = entry["host"], entry["key"]
    base = {
        "host": host,
        "key": key,
        "window_seconds": window_seconds,
        "decision_clock_ms": decision_clock_ms,
        "frozen": spec.is_frozen(host, key),
        "verdict": codes.D_NOMINAL,
        "code": None,
        "first_drift_ms": entry["first_drift_ms"],
        "quantization_band_ms": None,
        "margin_over_ms": None,
        "margin_inside_ms": None,
        "authoritative_generation": entry["authoritative"]["generation"],
        "authoritative_algebra": entry["authoritative"]["algebra"],
    }

    if entry["tombstoned"]:
        base["verdict"] = codes.D_REVOKED
        return base
    if not entry["drifted"] or entry["first_drift_ms"] is None:
        base["verdict"] = codes.D_NOMINAL
        return base

    first_drift_ms = entry["first_drift_ms"]
    quantum_ms = quantum_ms_by_source.get((host, key), 0)
    window_ms = int(window_seconds) * 1000
    center_age_ms = decision_clock_ms - first_drift_ms

    # Earliest plausible crossing -> maximum age; latest plausible -> minimum age.
    min_age_ms = center_age_ms - quantum_ms
    max_age_ms = center_age_ms + quantum_ms
    base["quantization_band_ms"] = {
        "quantum_ms": quantum_ms,
        "min_age_ms": min_age_ms,
        "max_age_ms": max_age_ms,
        "window_ms": window_ms,
    }

    if min_age_ms > window_ms and max_age_ms > window_ms:
        verdict, code = codes.D_IMMEDIATE, None
    elif max_age_ms <= window_ms:
        verdict, code = codes.D_GRACE, None
    else:
        verdict, code = codes.D_AMBIGUOUS, codes.E_WINDOW_AMBIGUOUS

    base["margin_over_ms"] = min_age_ms - window_ms
    base["margin_inside_ms"] = window_ms - max_age_ms
    base["verdict"] = codes.D_FROZEN if base["frozen"] else verdict
    base["code"] = code if not base["frozen"] else code
    return base


def adjudicate_all(all_evidence: list[Evidence], spec: BaselineSpec,
                   window_seconds: int, decision_clock_ms: int) -> dict:
    """Build the merged view and adjudicate every host/key deterministically."""
    view = merge_view(all_evidence, spec)

    # The quantization quantum for a (host, key) is the largest quantum among the
    # evidence that established its first crossing: v1 seconds => 1000 ms.
    quantum_by_source: dict[tuple[str, str], int] = {}
    for evidence in all_evidence:
        position = (evidence.host, evidence.canonical_key)
        quantum_by_source[position] = max(quantum_by_source.get(position, 0),
                                          evidence.time_quantum_ms)

    decisions = [
        adjudicate(entry, spec, window_seconds, decision_clock_ms, quantum_by_source)
        for entry in view["entries"]
    ]
    decisions.sort(key=lambda item: (item["host"], item["key"]))
    return {
        "decisions": decisions,
        "conflicts": view["conflicts"],
        "entries": view["entries"],
    }
