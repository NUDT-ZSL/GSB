"""End-to-end pipeline orchestration, degradation attribution,
cross-stage consistency self-check and deterministic report building.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from . import codes, errorprop, injection, points as points_mod
from . import ticks as ticks_mod
from .calibration import fit_calibration, invert, PRECISION_POLICY
from .errors import BusinessError
from .generator import load_truth
from .snapshots import SnapshotStore, canonical_json
from .sorting import sort_series_points

STAGES = ("tick_localization", "calibration", "point_localization",
          "inversion", "error_propagation", "sorting")


def _degraded(stage: str, triggering_code: str) -> Dict[str, Any]:
    return {
        "status": "degraded",
        "degradation": "stage_skipped",
        "triggered_by": triggering_code,
    }


def _match_points(points_result: Dict[str, Any], truth: Dict[str, Any],
                  calib: Dict[str, Any], cfg: Dict[str, Any]
                  ) -> Dict[str, Any]:
    """Invert detected pixels to values and pair them with truth points
    by ascending x rank inside each series."""
    chart_id = truth["chart_id"]
    series_out = []
    matched = []
    truth_by_series: Dict[str, List[Dict[str, Any]]] = {}
    for p in truth["points"]:
        truth_by_series.setdefault(p["series_id"], []).append(p)
    for lst in truth_by_series.values():
        lst.sort(key=lambda p: p["pixel_x"])

    for series in points_result["series"]:
        sid = series["series_id"]
        detected = sorted(series["points"], key=lambda c: c["pixel_x"])
        inverted_points = []
        for cand in detected:
            inv = invert(cand["pixel_y"], calib, cfg)
            inverted_points.append({
                "series_id": sid,
                "pixel_x": cand["pixel_x"],
                "pixel_y": cand["pixel_y"],
                "value": inv["value"],
                "clipped": inv["clipped"],
                "confidence": cand["confidence"],
            })
        truth_pts = truth_by_series.get(sid, [])
        for i, inv_pt in enumerate(inverted_points):
            if i < len(truth_pts):
                tp = truth_pts[i]
                matched.append({
                    **inv_pt,
                    "truth_pixel_x": tp["pixel_x"],
                    "truth_pixel_y": tp["pixel_y"],
                    "truth_value": tp["value"],
                })
        series_out.append({
            "series_id": sid,
            "points": inverted_points,
            "declared_count": len(truth_pts),
            "detected_count": len(inverted_points),
        })
    return {"chart_id": chart_id, "series": series_out, "matched": matched}


def _process_chart(truth: Dict[str, Any], samples_dir: Path,
                   cfg: Dict[str, Any], plan: injection.InjectionPlan
                   ) -> Dict[str, Any]:
    chart_id = truth["chart_id"]
    result: Dict[str, Any] = {
        "chart_id": chart_id,
        "chart_kind": truth["chart_kind"],
        "stages": {},
        "business_codes": [],
    }
    img = Image.open(samples_dir / truth["image_file"]).convert("RGB")
    failure: Optional[BusinessError] = None

    def degrade_from(stage: str) -> None:
        assert failure is not None
        result["business_codes"].append(failure.to_record())
        idx = STAGES.index(stage)
        result["stages"][stage] = {
            "status": "error",
            "code": failure.code,
            "message": failure.message,
        }
        for later in STAGES[idx + 1:]:
            result["stages"][later] = _degraded(later, failure.code)

    # Stage 1: tick localization (faults perturb inputs, not source code).
    tick_result = None
    try:
        tick_result = ticks_mod.locate_ticks(img, truth, cfg)
        injection.apply_tick_faults(plan, chart_id, tick_result)
        ticks_mod.validate_tick_pixels(tick_result, truth, cfg)
        ticks_mod.validate_tick_count(tick_result)
        result["stages"]["tick_localization"] = {"status": "ok", **tick_result}
    except BusinessError as exc:
        failure = exc
        degrade_from("tick_localization")

    # Stage 2: calibration.
    calib = None
    if failure is None:
        try:
            axis = dict(truth["axis"])
            injection.apply_calib_faults(plan, chart_id, axis)
            calib = fit_calibration(
                tick_result["tick_pixels"], list(axis["tick_values"]),
                axis["axis_min"], axis["axis_max"], chart_id, cfg)
            result["stages"]["calibration"] = {"status": "ok", **calib}
        except BusinessError as exc:
            failure = exc
            degrade_from("calibration")

    # Stage 3: point localization.
    points_result = None
    if failure is None:
        try:
            points_result = points_mod.locate_points(img, truth, cfg)
            injection.apply_point_faults(plan, chart_id, points_result)
            points_mod.validate_points_result(points_result, truth, cfg)
            result["stages"]["point_localization"] = {
                "status": "ok", **points_result}
        except BusinessError as exc:
            failure = exc
            degrade_from("point_localization")

    # Stage 4: inversion (duplicate series ids are rejected here).
    inversion_result = None
    if failure is None:
        try:
            injection.apply_inversion_faults(plan, chart_id,
                                             points_result["series"])
            ids = [s["series_id"] for s in points_result["series"]]
            if len(ids) != len(set(ids)):
                dup = next(s for s in ids if ids.count(s) > 1)
                raise BusinessError(
                    codes.E_DUPLICATE_SERIES,
                    f"duplicate series id {dup}",
                    chart_id=chart_id, stage="inversion", series_id=dup,
                )
            inversion_result = _match_points(points_result, truth, calib, cfg)
            result["stages"]["inversion"] = {
                "status": "ok",
                "series": inversion_result["series"],
            }
        except BusinessError as exc:
            failure = exc
            degrade_from("inversion")

    # Stage 5: error propagation.
    if failure is None:
        try:
            spectrum = errorprop.compute_error_spectrum(
                chart_id, truth, tick_result, calib,
                inversion_result["matched"], cfg)
            errorprop.traceability_check(spectrum, calib["slope"], chart_id)
            result["stages"]["error_propagation"] = {"status": "ok", **spectrum}
        except BusinessError as exc:
            failure = exc
            degrade_from("error_propagation")

    # Stage 6: stable sorting + thresholding.
    if failure is None:
        sorted_series = [
            {"series_id": s["series_id"],
             "ordered": sort_series_points(s["points"], cfg)}
            for s in inversion_result["series"]
        ]
        result["stages"]["sorting"] = {
            "status": "ok",
            "strategy": cfg["threshold_strategy"],
            "series": sorted_series,
        }

    return result


def _self_check(report_charts: List[Dict[str, Any]], cfg: Dict[str, Any]
                ) -> List[Dict[str, Any]]:
    violations: List[Dict[str, Any]] = []
    atol = cfg["recompute_atol"]
    for chart in report_charts:
        chart_id = chart["chart_id"]
        stages = chart["stages"]
        missing = [s for s in STAGES if s not in stages]
        if missing:
            violations.append({
                "code": codes.E_INCONSISTENT,
                "category": "chart_id_set",
                "chart_id": chart_id,
                "detail": f"stages missing without record: {missing}",
            })
            continue
        calib = stages.get("calibration", {})
        inv = stages.get("inversion", {})
        if calib.get("status") == "ok" and inv.get("status") == "ok":
            slope, intercept = calib["slope"], calib["intercept"]
            for series in inv["series"]:
                for p in series["points"]:
                    recomputed = slope * p["pixel_y"] + intercept
                    if cfg["clip_to_axis_domain"]:
                        recomputed = min(max(recomputed, calib["axis_min"]),
                                         calib["axis_max"])
                    if abs(recomputed - p["value"]) > atol:
                        violations.append({
                            "code": codes.E_INCONSISTENT,
                            "category": "inversion_recompute",
                            "chart_id": chart_id,
                            "detail": f"series {series['series_id']} point "
                                      f"({p['pixel_x']}, {p['pixel_y']}): "
                                      f"{p['value']} != {recomputed}",
                        })
        spectrum = stages.get("error_propagation", {})
        if spectrum.get("status") == "ok" and calib.get("status") == "ok":
            try:
                errorprop.traceability_check(spectrum, calib["slope"], chart_id)
            except BusinessError as exc:
                violations.append({
                    "code": codes.E_INCONSISTENT,
                    "category": "error_interval",
                    "chart_id": chart_id,
                    "detail": exc.message,
                })
    return violations


def run_pipeline(samples_dir: str | Path, cfg: Dict[str, Any],
                 plan: Optional[injection.InjectionPlan] = None,
                 snapshot_store: Optional[SnapshotStore] = None
                 ) -> Dict[str, Any]:
    samples_dir = Path(samples_dir)
    plan = plan or injection.InjectionPlan([])
    truths = load_truth(samples_dir)

    charts: List[Dict[str, Any]] = []
    unattributed: List[Dict[str, Any]] = []
    for truth in truths:
        try:
            charts.append(_process_chart(truth, samples_dir, cfg, plan))
        except BusinessError as exc:  # stage-level escape: attribute it
            unattributed.append({"chart_id": truth["chart_id"],
                                 "code": exc.code, "message": exc.message})
        except Exception as exc:  # noqa: BLE001 - unattributed anomaly path
            unattributed.append({
                "chart_id": truth.get("chart_id"),
                "exception_type": type(exc).__name__,
                "message": str(exc),
            })

    violations = _self_check(charts, cfg)

    # Snapshot handling: load previous config snapshot to flag strategy
    # changes, then persist whole-replacement snapshots of the three kinds.
    snapshot_events: List[Dict[str, Any]] = []
    snapshot_codes: List[Dict[str, Any]] = []
    strategy_changed = False
    if snapshot_store is not None:
        try:
            previous = snapshot_store.load("config")
            if previous is not None:
                old_strategy = previous["payload"].get("threshold_strategy")
                strategy_changed = old_strategy != cfg["threshold_strategy"]
        except BusinessError as exc:
            snapshot_codes.append(exc.to_record())

        spectra = [c["stages"]["error_propagation"] for c in charts
                   if c["stages"].get("error_propagation", {}).get("status") == "ok"]
        for category, payload in (
            ("config", cfg),
            ("ground_truth", truths),
            ("error_stats", spectra),
        ):
            try:
                rec = snapshot_store.save(category, payload)
                snapshot_events.append({
                    "category": category,
                    "version": rec["version"],
                    "checksum": rec["checksum"],
                    "replacement": "whole",
                })
            except BusinessError as exc:
                snapshot_codes.append(exc.to_record())

    all_codes: List[Dict[str, Any]] = []
    for chart in charts:
        all_codes.extend(chart["business_codes"])
    all_codes.extend(snapshot_codes)
    all_codes.extend(violations)

    report: Dict[str, Any] = {
        "report_version": 1,
        "seed": cfg["seed"],
        "config": cfg,
        "precision_policy": {
            "calibration": PRECISION_POLICY,
            "error_stats_significant_digits": cfg["error_stats_precision"],
            "inverted_values_rounding": "none",
        },
        "threshold_strategy": cfg["threshold_strategy"],
        "strategy_changed": strategy_changed,
        "charts": charts,
        "injections": plan.records,
        "unattributed_anomalies": unattributed,
        "snapshots": snapshot_events,
        "business_codes": all_codes,
        "self_check": {
            "status": "failed" if violations else "ok",
            "violations": violations,
        },
    }
    digest = hashlib.sha256(
        canonical_json(report).encode("utf-8")).hexdigest()
    report["report_sha256"] = digest
    return report


def write_report(report: Dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8", newline="\n")


def report_hash(path: str | Path) -> str:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.pop("report_sha256", None)
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
