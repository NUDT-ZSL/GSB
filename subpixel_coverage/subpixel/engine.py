"""High-level facade orchestrating the full sub-pixel coverage pipeline."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .contour import ContourReport, check_continuity, report_to_dict
from .incremental import (
    CurveAdjustment,
    DensityAdjustment,
    IncrementalResult,
    apply_adjustments,
    incremental_recompute,
    report_to_dict as incremental_report_to_dict,
)
from .materials import material_masks, material_statistics, stats_to_dicts
from .models import PackagingProject, summary
from .persistence import CoverageStore, StoredCommit, content_hash
from .rasterizer import (
    RasterResult,
    rasterize_polygon,
    rasterize_strip,
    rasterize_supersample,
)
from .thumbnail import ThumbnailResult, build_thumbnail, declared_thumbnail_size
from .validation import validate_project

DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "coverage.sqlite3",
)


@dataclass
class EngineState:
    project: PackagingProject
    full: RasterResult
    reference: RasterResult
    thumbnail: ThumbnailResult
    masks: Dict
    material_stats: List[Dict]
    contour: ContourReport
    commit: Optional[StoredCommit] = None


class CoverageEngine:
    """Stateful service object used by the Flask demo and the CLI scripts."""

    def __init__(self, project: PackagingProject, db_path: str = DEFAULT_DB):
        self.project = validate_project(project)
        self.db_path = db_path
        self.store = CoverageStore(db_path)
        self.state: Optional[EngineState] = None
        self.incremental_report: Optional[IncrementalResult] = None

    @classmethod
    def from_project_file(cls, path: str, db_path: str = DEFAULT_DB) -> "CoverageEngine":
        from .generator import read_project
        return cls(read_project(path), db_path)

    def close(self) -> None:
        self.store.close()

    def summary(self) -> Dict:
        return summary(self.project)

    def run_full(self, thumb_factor: int = 4,
                 continuity_threshold: float = 0.05) -> EngineState:
        full = rasterize_strip(self.project, self.project.camera)
        reference = rasterize_polygon(self.project, self.project.camera)
        thumb_w, thumb_h = declared_thumbnail_size(
            self.project.camera.width_px, self.project.camera.height_px, thumb_factor)
        thumbnail = build_thumbnail(self.project, full, thumb_w, thumb_h)
        masks = material_masks(self.project, self.project.camera)
        stats = material_statistics(
            self.project, full.coverage, full.contributions, masks)
        contour = check_continuity(self.project, full, continuity_threshold)
        self.state = EngineState(
            project=self.project,
            full=full,
            reference=reference,
            thumbnail=thumbnail,
            masks=masks,
            material_stats=stats_to_dicts(stats),
            contour=contour,
        )
        return self.state

    def reference_comparison(self, atol: float = 0.02) -> Dict:
        if self.state is None:
            raise RuntimeError("call run_full() first")
        primary = self.state.full.coverage
        reference = self.state.reference.coverage
        diff = np_absdiff(primary, reference)
        return {
            "method_primary": self.state.full.method,
            "method_reference": self.state.reference.method,
            "max_abs_error": float(diff.max()),
            "mean_abs_error": float(diff.mean()),
            "pixels_over_tolerance": int((diff > atol).sum()),
            "tolerance": atol,
            "within_tolerance": bool(diff.max() <= atol),
        }

    def supersample_crosscheck(self, atol: float = 0.15,
                               samples_per_axis: int = 16) -> Dict:
        """Stochastic cross-check.

        Sub-pixel grains cover only a few sample points, so the per-pixel
        Monte-Carlo noise is bounded by ``atol`` while the mean error stays
        orders of magnitude smaller; analytic-vs-analytic remains the formal
        acceptance comparison.
        """
        result = rasterize_supersample(self.project, self.project.camera,
                                       samples_per_axis=samples_per_axis)
        diff = np_absdiff(self.state.full.coverage, result.coverage)
        return {
            "method": result.method,
            "max_abs_error": float(diff.max()),
            "mean_abs_error": float(diff.mean()),
            "pixels_over_tolerance": int((diff > atol).sum()),
            "tolerance": atol,
            "within_tolerance": bool(diff.max() <= atol),
        }

    def commit(self, note: str = "full render") -> StoredCommit:
        if self.state is None:
            self.run_full()
        assert self.state is not None
        latest = self.store.latest_commit(self.project.project_id)
        if latest is not None and latest.content_hash != content_hash(self.project):
            note = f"{note} [version advanced {latest.version} -> {self.project.version}]"
        self.state.commit = self.store.save_commit(
            self.project, self.state.full, self.state.thumbnail,
            self.state.material_stats, note=note)
        return self.state.commit

    def resume(self) -> Optional[StoredCommit]:
        """Reload the newest committed artifacts when the version still matches."""
        commit = self.store.latest_commit(self.project.project_id)
        self.store.assert_version_current(self.project, commit)
        if commit is None:
            return None
        coverage, thumbnail, contributions = self.store.load_commit_artifacts(
            commit.commit_id)
        state = self.run_full()
        state.full.coverage = coverage
        state.full.contributions = contributions
        state.full.touched = coverage > 0.0
        state.thumbnail.averaged = thumbnail
        state.commit = commit
        return commit

    def adjust(self, curve_changes: Sequence[CurveAdjustment] = (),
               density_changes: Sequence[DensityAdjustment] = (),
               thumb_factor: int = 4) -> Tuple[PackagingProject, IncrementalResult]:
        if self.state is None:
            self.run_full(thumb_factor=thumb_factor)
        assert self.state is not None
        updated, changed_ids = apply_adjustments(
            self.project, curve_changes, density_changes)
        updated = validate_project(updated)
        result, report = incremental_recompute(
            updated, self.state.full, changed_ids,
            thumb_factor_x=thumb_factor, thumb_factor_y=thumb_factor,
        )
        thumb_w, thumb_h = declared_thumbnail_size(
            updated.camera.width_px, updated.camera.height_px, thumb_factor)
        thumbnail = build_thumbnail(updated, result, thumb_w, thumb_h)
        masks = material_masks(updated, updated.camera)
        stats = material_statistics(
            updated, result.coverage, result.contributions, masks)
        contour = check_continuity(updated, result)
        self.project = updated
        self.state = EngineState(
            project=updated,
            full=result,
            reference=self.state.reference,
            thumbnail=thumbnail,
            masks=masks,
            material_stats=stats_to_dicts(stats),
            contour=contour,
            commit=self.state.commit,
        )
        self.incremental_report = report
        return updated, report

    def fiber_detail(self, curve_id: str) -> Optional[Dict]:
        if self.state is None:
            return None
        curve = next((item for item in self.project.curves
                      if item.curve_id == curve_id), None)
        if curve is None:
            return None
        rows = self.state.full.contributions[
            self.state.full.contributions["curve_id"] == curve_id]
        return {
            "curve_id": curve.curve_id,
            "kind": curve.kind,
            "material": curve.material,
            "surface": curve.surface,
            "radius_px": curve.radius_px,
            "opacity": curve.opacity,
            "declared_length": curve.declared_length,
            "start_2d": list(curve.start_2d),
            "tangent_2d": list(curve.tangent_2d),
            "rim_guard": curve.rim_guard,
            "pixel_count": int(len(rows)),
            "total_area": float(rows["area"].sum()) if len(rows) else 0.0,
            "total_alpha": float(rows["alpha"].sum()) if len(rows) else 0.0,
            "pixels": [
                {"x": int(row["x"]), "y": int(row["y"]),
                 "area": float(row["area"]), "alpha": float(row["alpha"])}
                for row in rows[:500]
            ],
        }

    def contour_payload(self) -> Dict:
        if self.state is None:
            raise RuntimeError("call run_full() first")
        return report_to_dict(self.state.contour)

    def incremental_payload(self) -> Optional[Dict]:
        if self.incremental_report is None:
            return None
        return incremental_report_to_dict(self.incremental_report)


def np_absdiff(a, b):
    import numpy as np
    return np.abs(a - b)
