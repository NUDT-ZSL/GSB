"""Fault injection driven purely by an external parameter file.

No source code is modified to create faults: the pipeline consults the
injection plan at each stage boundary and perturbs the *inputs* of that
stage (tick pixels, axis range, candidate coordinates, series ids).
Every triggered fault yields a record with stage, business code and
chart_id; downstream stages receive a degradation marker naming the
upstream business code that caused it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import codes

STAGE_TICK = "tick_localization"
STAGE_CALIB = "calibration"
STAGE_POINT = "point_localization"
STAGE_INVERSION = "inversion"

FAULT_KINDS = {
    "tick_pixel_out_of_bounds": (STAGE_TICK, codes.E_TICK_LOC_INVALID),
    "axis_range_non_positive": (STAGE_CALIB, codes.E_AXIS_RANGE_INVALID),
    "point_outside_canvas": (STAGE_POINT, codes.E_POINT_OUT_OF_CANVAS),
    "duplicate_series_id": (STAGE_INVERSION, codes.E_DUPLICATE_SERIES),
}


class InjectionPlan:
    def __init__(self, faults: List[Dict[str, Any]]) -> None:
        self.faults = faults
        self.records: List[Dict[str, Any]] = []

    @classmethod
    def load(cls, path: Optional[str | Path]) -> "InjectionPlan":
        if not path:
            return cls([])
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(list(data.get("faults", [])))

    def faults_for(self, stage: str, chart_id: str) -> List[Dict[str, Any]]:
        return [f for f in self.faults
                if FAULT_KINDS.get(f.get("kind"), (None, None))[0] == stage
                and f.get("chart_id") == chart_id]

    def record(self, fault: Dict[str, Any], chart_id: str) -> Dict[str, Any]:
        stage, code = FAULT_KINDS[fault["kind"]]
        rec = {
            "fault_id": fault.get("id", f"fault_{len(self.records)}"),
            "stage": stage,
            "kind": fault["kind"],
            "code": code,
            "chart_id": chart_id,
        }
        self.records.append(rec)
        return rec


def apply_tick_faults(plan: InjectionPlan, chart_id: str,
                      tick_result: Dict[str, Any]) -> None:
    for fault in plan.faults_for(STAGE_TICK, chart_id):
        idx = fault.get("params", {}).get("tick_index", 0)
        offset = fault.get("params", {}).get("offset", -1000.0)
        if tick_result["tick_pixels"]:
            i = min(idx, len(tick_result["tick_pixels"]) - 1)
            tick_result["tick_pixels"][i] += offset
        plan.record(fault, chart_id)


def apply_calib_faults(plan: InjectionPlan, chart_id: str,
                       axis: Dict[str, Any]) -> None:
    for fault in plan.faults_for(STAGE_CALIB, chart_id):
        axis["axis_max"] = axis["axis_min"]
        plan.record(fault, chart_id)


def apply_point_faults(plan: InjectionPlan, chart_id: str,
                       points_result: Dict[str, Any]) -> None:
    for fault in plan.faults_for(STAGE_POINT, chart_id):
        params = fault.get("params", {})
        target = params.get("series_id")
        for series in points_result["series"]:
            if target is None or series["series_id"] == target:
                series["points"].append({
                    "pixel_x": -5.0,
                    "pixel_y": -5.0,
                    "confidence": 0.0,
                    "merged_count": 1,
                    "injected": True,
                })
        plan.record(fault, chart_id)


def apply_inversion_faults(plan: InjectionPlan, chart_id: str,
                           series_list: List[Dict[str, Any]]) -> None:
    for fault in plan.faults_for(STAGE_INVERSION, chart_id):
        params = fault.get("params", {})
        target = params.get("series_id")
        for series in list(series_list):
            if target is None or series["series_id"] == target:
                series_list.append(dict(series))
                break
        plan.record(fault, chart_id)
