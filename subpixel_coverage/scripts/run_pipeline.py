"""Run the full pipeline, print diagnostics and commit results to SQLite."""
from __future__ import annotations

import argparse
import json
import os

from subpixel.engine import CoverageEngine, DEFAULT_DB
from subpixel.incremental import CurveAdjustment, DensityAdjustment
from subpixel.models import summary


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.path.join(root, "data", "packaging_project.json"))
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--commit-note", default="full baseline render")
    args = parser.parse_args()

    engine = CoverageEngine.from_project_file(args.project, args.db)
    engine.run_full()

    print("== project summary ==")
    print(json.dumps(summary(engine.project), ensure_ascii=False, indent=2))

    print("== analytic reference comparison ==")
    print(json.dumps(engine.reference_comparison(), indent=2))

    print("== supersampling cross-check ==")
    print(json.dumps(engine.supersample_crosscheck(), indent=2))

    print("== material statistics ==")
    print(json.dumps(engine.state.material_stats, indent=2, ensure_ascii=False))

    print("== thumbnail conservation ==")
    thumb = engine.state.thumbnail
    print(json.dumps({
        "full_total": thumb.full_total,
        "thumb_total_scaled": thumb.thumb_total,
        "conservation_error": thumb.conservation_error,
        "attenuation_pixels": int(thumb.attenuation.sum()),
        "fracture_pixels": int(thumb.fracture.sum()),
    }, indent=2))

    print("== contour diagnostics ==")
    print(json.dumps(engine.contour_payload(), indent=2, ensure_ascii=False))

    commit = engine.commit(note=args.commit_note)
    print(f"== committed {commit.commit_id} version={commit.version} ==")

    print("== incremental recompute demo ==")
    updated, report = engine.adjust(
        curve_changes=[CurveAdjustment("FB-0001", radius_px=1.35, opacity=0.97)],
        density_changes=[DensityAdjustment(
            kind="grain", material="frosted_glass", surface="body",
            region=(-1.8, 0.4, -0.6, 1.2), add=6, remove=2)],
    )
    print(json.dumps(engine.incremental_payload(), indent=2, ensure_ascii=False))
    inc_commit = engine.commit(note="incremental adjustment")
    print(f"== committed {inc_commit.commit_id} version={inc_commit.version} ==")
    print("== version drift guard demo ==")
    try:
        stale = engine.store.latest_commit(engine.project.project_id)
        stale.content_hash = "0" * 64
        engine.store.assert_version_current(engine.project, stale)
    except ValueError as exc:
        print(f"correctly rejected stale coverage: {exc}")
    engine.close()


if __name__ == "__main__":
    main()
