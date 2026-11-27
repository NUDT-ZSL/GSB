"""Generate the deterministic packaging project and reference coverage data."""
from __future__ import annotations

import argparse
import json
import os

from subpixel.generator import ENGINE_VERSION, generate_project, write_project
from subpixel.rasterizer import rasterize_polygon, rasterize_strip
from subpixel.validation import validate_project


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
    args = parser.parse_args()
    os.makedirs(args.data_dir, exist_ok=True)

    project = validate_project(generate_project())
    project_path = os.path.join(args.data_dir, "packaging_project.json")
    write_project(project, project_path)

    primary = rasterize_strip(project, project.camera)
    reference = rasterize_polygon(project, project.camera)
    reference_payload = {
        "engine_version": ENGINE_VERSION,
        "method_primary": primary.method,
        "method_reference": reference.method,
        "resolution": [primary.width, primary.height],
        "full_total_primary": float(primary.coverage.sum()),
        "full_total_reference": float(reference.coverage.sum()),
        "max_abs_error": float(abs(primary.coverage - reference.coverage).max()),
        "curve_count": len(project.curves),
        "invalid_curve_ids": sorted({issue.curve_id for issue in project.issues}),
    }
    ref_path = os.path.join(args.data_dir, "reference_coverage.json")
    with open(ref_path, "w", encoding="utf-8") as handle:
        json.dump(reference_payload, handle, ensure_ascii=False, indent=2)

    print(f"project written : {project_path}")
    print(f"reference data  : {ref_path}")
    print(json.dumps(reference_payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
