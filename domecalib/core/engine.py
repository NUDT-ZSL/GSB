"""Calibration engine: orchestrates LUT, TPS, blending, versions and caching."""

from __future__ import annotations

import copy

import numpy as np

from .blending import build_blend_masks
from .errors import CalibrationError, E_VERSION_CONFLICT
from .mapping import build_projector_lut, compare_lut_with_truth
from .models import (
    CalibrationProject,
    stable_hash,
)
from .render import alignment_report, brightness_report, render_preview

DEFAULT_TRUTH_TOLERANCE_RAD = 1e-6


def _spec_hash(spec) -> str:
    return stable_hash(spec.signature())


def _points_hash(points) -> str:
    if points is None:
        return stable_hash({"none": True})
    return stable_hash(points.to_dict())


def _blend_hash(project: CalibrationProject) -> str:
    payload = {"blending": project.blending.signature(),
               "grid": [project.grid_width, project.grid_height],
               "layout": [s.projector_id for s in project.projectors]}
    return stable_hash(payload)


class CalibrationEngine:
    """Stateful engine that keeps cached artefacts consistent with inputs."""

    def __init__(self, project: CalibrationProject):
        self.project = project

    # ---- hashes ------------------------------------------------------------
    def spec_hashes(self) -> dict[str, str]:
        return {s.projector_id: _spec_hash(s) for s in self.project.projectors}

    def points_hashes(self) -> dict[str, str]:
        return {
            s.projector_id: _points_hash(self.project.points_for(s.projector_id))
            for s in self.project.projectors
        }

    # ---- LUT building ------------------------------------------------------
    def _build_one_lut(self, projector_id: str, blend_hash: str) -> None:
        spec = self.project.projector(projector_id)
        points = self.project.points_for(projector_id)
        version = self.project.projector_versions.get(projector_id, 0) + 1
        lut, quality = build_projector_lut(
            spec, self.project.dome, points,
            spec_hash=_spec_hash(spec),
            points_hash=_points_hash(points),
            blend_hash=blend_hash,
            version=version,
        )
        self.project.luts[projector_id] = lut
        if quality is not None:
            self.project.quality[projector_id] = quality
        self.project.projector_versions[projector_id] = version

    def rebuild_luts(self, projector_ids: list[str] | None = None) -> list[str]:
        """Rebuild LUTs for the given ids (or all). Returns rebuilt ids."""
        blend_hash = _blend_hash(self.project)
        if projector_ids is None:
            projector_ids = [s.projector_id for s in self.project.projectors]
        rebuilt = []
        for pid in projector_ids:
            cached = self.project.luts.get(pid)
            spec = self.project.projector(pid)
            points = self.project.points_for(pid)
            if (
                cached is not None
                and cached.spec_hash == _spec_hash(spec)
                and cached.points_hash == _points_hash(points)
            ):
                continue
            self._build_one_lut(pid, blend_hash)
            rebuilt.append(pid)
        return rebuilt

    def rebuild_blending(self) -> dict:
        """Rebuild all blend masks from current LUTs."""
        if not self.project.luts:
            self.rebuild_luts()
        blend_hash = _blend_hash(self.project)
        masks, diagnostics = build_blend_masks(self.project, self.project.luts)
        for pid, mask in masks.items():
            mask.blend_hash = blend_hash
            mask.version = self.project.projector_versions.get(pid, 1)
        self.project.masks = masks
        for lut in self.project.luts.values():
            lut.blend_hash = blend_hash
        return diagnostics

    def full_rebuild(self) -> dict:
        """Force a complete rebuild and return the full report bundle."""
        self.project.luts.clear()
        self.project.masks.clear()
        self.rebuild_luts()
        blend_diag = self.rebuild_blending()
        return self.run_reports(blend_diag)

    def update_blending_params(self, feather_width_deg: float | None = None,
                               curve: str | None = None,
                               gamma: float | None = None) -> dict:
        """Change feather parameters; only blend masks need recomputation."""
        current = self.project.blending
        if feather_width_deg is not None:
            current.feather_width_deg = float(feather_width_deg)
        if curve is not None:
            current.curve = str(curve)
        if gamma is not None:
            current.gamma = float(gamma)
        blend_diag = self.rebuild_blending()
        return self.run_reports(blend_diag)

    def update_projector(self, projector_id: str, **changes) -> dict:
        """Modify one projector's spec and rebuild it plus shared blending."""
        spec = self.project.projector(projector_id)
        for key, value in changes.items():
            if not hasattr(spec, key):
                raise CalibrationError(
                    "E1006_INVALID_PARAMETER",
                    f"projector has no field '{key}'",
                    {"projector_id": projector_id, "field": key},
                )
            setattr(spec, key, value)
        # Resolution changes must match any stored LUT; the rebuild replaces it.
        self.rebuild_luts([projector_id])
        blend_diag = self.rebuild_blending()
        return self.run_reports(blend_diag)

    def update_control_points(self, projector_id: str,
                              observed_lon: np.ndarray,
                              observed_lat: np.ndarray) -> dict:
        """Replace one projector's observed points and rebuild only that one."""
        points = self.project.points_for(projector_id)
        if points is None or points.count() != observed_lon.size:
            raise CalibrationError(
                "E1005_RESOLUTION_MISMATCH",
                "control point count must stay consistent with the sample",
                {"projector_id": projector_id,
                 "expected": 0 if points is None else points.count(),
                 "got": int(observed_lon.size)},
            )
        points.observed_lon = np.asarray(observed_lon, dtype=float)
        points.observed_lat = np.asarray(observed_lat, dtype=float)
        self.rebuild_luts([projector_id])
        blend_diag = self.rebuild_blending()
        return self.run_reports(blend_diag)

    # ---- reports / textures ------------------------------------------------
    def run_reports(self, blend_diag: dict | None = None,
                    tolerance_rad: float = DEFAULT_TRUTH_TOLERANCE_RAD) -> dict:
        if blend_diag is None:
            blend_diag = self.rebuild_blending()
        truth_errors = {}
        for spec in self.project.projectors:
            lut = self.project.luts[spec.projector_id]
            truth_errors[spec.projector_id] = compare_lut_with_truth(
                lut, spec, self.project.dome
            )
        bright = brightness_report(
            self.project, self.project.luts, self.project.masks
        )
        align = alignment_report(
            self.project, self.project.luts, self.project.quality,
            truth_errors, tolerance_rad
        )
        quality_summary = {
            pid: q.to_dict() for pid, q in self.project.quality.items()
        }
        return {
            "blend_diagnostics": blend_diag,
            "brightness": bright,
            "alignment": align,
            "tps_quality": quality_summary,
            "versions": dict(self.project.projector_versions),
        }

    def composite_textures(self) -> dict[str, np.ndarray]:
        """Per-projector mapping textures stacked as (H, W, 4): lon, lat, w, valid."""
        textures = {}
        for pid, lut in self.project.luts.items():
            mask = self.project.masks[pid]
            valid = lut.valid.astype(float)
            texture = np.stack(
                [np.nan_to_num(lut.lon), np.nan_to_num(lut.lat),
                 mask.weight, valid], axis=-1
            )
            textures[pid] = texture
        return textures

    def preview(self, pattern: str = "checker"):
        return render_preview(
            self.project, self.project.luts, self.project.masks, pattern
        )

    # ---- version guard -----------------------------------------------------
    def assert_versions_fresh(self, expected_versions: dict[str, int]) -> None:
        """Reject loading stale artefacts whose projector versions drifted."""
        for pid, expected in expected_versions.items():
            current = self.project.projector_versions.get(pid)
            if current != expected:
                raise CalibrationError(
                    E_VERSION_CONFLICT,
                    f"stale data for {pid}: snapshot v{expected} != current v{current}",
                    {"projector_id": pid,
                     "snapshot_version": expected,
                     "current_version": current},
                )

    def clone(self) -> "CalibrationEngine":
        return CalibrationEngine(copy.deepcopy(self.project))
