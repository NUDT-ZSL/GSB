"""Local Flask demo service for the sub-pixel coverage engine."""
from __future__ import annotations

import os
from typing import List, Tuple

import numpy as np
from flask import Flask, Response, jsonify, render_template, request

from .engine import CoverageEngine, DEFAULT_DB
from .image import (
    coverage_to_rgb,
    encode_png,
    overlay_pixels,
    side_by_side,
    upscale_rgb,
)
from .incremental import CurveAdjustment, DensityAdjustment


def create_app(project_path: str, db_path: str = DEFAULT_DB) -> Flask:
    app = Flask(__name__)
    engine = CoverageEngine.from_project_file(project_path, db_path)
    engine.run_full()
    app.config["ENGINE"] = engine

    def current_engine() -> CoverageEngine:
        return app.config["ENGINE"]

    @app.context_processor
    def inject_summary():
        return {"summary": current_engine().summary()}

    @app.route("/")
    def dashboard():
        eng = current_engine()
        reference = eng.reference_comparison()
        return render_template(
            "dashboard.html",
            summary=eng.summary(),
            reference=reference,
            stats=eng.state.material_stats,
            thumbnail=eng.state.thumbnail,
            contour=eng.contour_payload(),
            incremental=eng.incremental_payload(),
            commit=(eng.state.commit.__dict__ if eng.state.commit else None),
        )

    @app.route("/api/summary")
    def api_summary():
        eng = current_engine()
        return jsonify({
            "summary": eng.summary(),
            "reference": eng.reference_comparison(),
            "materials": eng.state.material_stats,
            "thumbnail_conservation": {
                "full_total": eng.state.thumbnail.full_total,
                "thumb_total_scaled": eng.state.thumbnail.thumb_total,
                "error": eng.state.thumbnail.conservation_error,
                "attenuation_pixels": int(eng.state.thumbnail.attenuation.sum()),
                "fracture_pixels": int(eng.state.thumbnail.fracture.sum()),
            },
            "contour": eng.contour_payload(),
            "incremental": eng.incremental_payload(),
        })

    @app.route("/image/coverage.png")
    def image_coverage():
        eng = current_engine()
        rgb = coverage_to_rgb(eng.state.full.coverage)
        return Response(encode_png(rgb), mimetype="image/png")

    @app.route("/image/reference.png")
    def image_reference():
        eng = current_engine()
        rgb = coverage_to_rgb(eng.state.reference.coverage)
        return Response(encode_png(rgb), mimetype="image/png")

    @app.route("/image/contour.png")
    def image_contour():
        eng = current_engine()
        rgb = coverage_to_rgb(eng.state.full.coverage)
        break_pixels = [(item.x, item.row) for item in eng.state.contour.breaks]
        rgb = overlay_pixels(rgb, break_pixels, color=(255, 70, 70))
        return Response(encode_png(rgb), mimetype="image/png")

    @app.route("/image/thumbnail.png")
    def image_thumbnail():
        eng = current_engine()
        thumb = eng.state.thumbnail
        left = coverage_to_rgb(thumb.averaged)
        right = coverage_to_rgb(thumb.direct)
        mark_pixels: List[Tuple[int, int]] = [
            (int(x), int(y))
            for y, x in zip(*np.nonzero(thumb.fracture | thumb.attenuation))
        ]
        right = overlay_pixels(right, mark_pixels, color=(255, 70, 70))
        factor = max(4, 320 // max(thumb.thumb_width, 1))
        merged = side_by_side(upscale_rgb(left, factor), upscale_rgb(right, factor))
        return Response(encode_png(merged), mimetype="image/png")

    @app.route("/fibers")
    def fibers_view():
        eng = current_engine()
        curve_ids = [curve.curve_id for curve in eng.project.valid_curves()]
        selected = request.args.get("curve_id", curve_ids[0])
        detail = eng.fiber_detail(selected)
        return render_template(
            "fibers.html",
            curve_ids=curve_ids,
            selected=selected,
            detail=detail,
        )

    @app.route("/api/fibers/<curve_id>")
    def api_fiber(curve_id: str):
        detail = current_engine().fiber_detail(curve_id)
        if detail is None:
            return jsonify({"error": f"unknown curve {curve_id}"}), 404
        return jsonify(detail)

    @app.route("/thumbnail")
    def thumbnail_view():
        eng = current_engine()
        thumb = eng.state.thumbnail
        return render_template(
            "thumbnail.html",
            meta={
                "thumb_width": thumb.thumb_width,
                "thumb_height": thumb.thumb_height,
                "factor": [thumb.factor_x, thumb.factor_y],
                "tolerance": thumb.tolerance,
                "full_total": thumb.full_total,
                "thumb_total_scaled": thumb.thumb_total,
                "direct_total": thumb.direct_total,
                "conservation_error": thumb.conservation_error,
                "attenuation_pixels": int(thumb.attenuation.sum()),
                "fracture_pixels": int(thumb.fracture.sum()),
                "marked_pixels": thumb.marked_pixels[:100],
            },
        )

    @app.route("/contour")
    def contour_view():
        eng = current_engine()
        return render_template(
            "contour.html",
            report=eng.contour_payload(),
        )

    @app.route("/api/recompute", methods=["GET", "POST"])
    def api_recompute():
        eng = current_engine()
        source = request.args if request.method == "GET" else (
            request.get_json(silent=True) or request.form)
        curve_changes = []
        for raw in source.getlist("curve") if hasattr(source, "getlist") else source.get("curve", []):
            parts = str(raw).split(":")
            curve_id = parts[0]
            radius = float(parts[1]) if len(parts) > 1 and parts[1] else None
            opacity = float(parts[2]) if len(parts) > 2 and parts[2] else None
            curve_changes.append(CurveAdjustment(curve_id, radius, opacity))
        density_changes = []
        for raw in source.getlist("density") if hasattr(source, "getlist") else source.get("density", []):
            kind, material, surface, u0, v0, u1, v1, add, remove = str(raw).split(":")
            density_changes.append(DensityAdjustment(
                kind=kind, material=material, surface=surface,
                region=tuple(float(v) for v in (u0, v0, u1, v1)),
                add=int(add), remove=int(remove),
            ))
        updated, report = eng.adjust(curve_changes, density_changes)
        job_id = eng.store.create_job(
            updated.project_id,
            eng.state.commit.commit_id if eng.state.commit else "uncommitted",
            updated.version,
            {"curve": source.getlist("curve") if hasattr(source, "getlist") else source.get("curve", []),
             "density": source.getlist("density") if hasattr(source, "getlist") else source.get("density", [])},
        )
        payload = {
            "job_id": job_id,
            "version": updated.version,
            "recompute": eng.incremental_payload(),
            "reference": eng.reference_comparison(),
            "contour": eng.contour_payload(),
            "materials": eng.state.material_stats,
        }
        eng.store.finish_job(job_id, payload["recompute"])
        return jsonify(payload)

    @app.route("/api/commit", methods=["POST"])
    def api_commit():
        eng = current_engine()
        note = request.args.get("note", "incremental commit")
        commit = eng.commit(note=note)
        return jsonify({
            "commit_id": commit.commit_id,
            "version": commit.version,
            "full_total": commit.full_total,
            "thumb_total_scaled": commit.thumb_total_scaled,
            "conservation_error": commit.conservation_error,
        })

    @app.route("/api/resume", methods=["POST"])
    def api_resume():
        eng = current_engine()
        commit = eng.resume()
        if commit is None:
            return jsonify({"resumed": False})
        return jsonify({
            "resumed": True,
            "commit_id": commit.commit_id,
            "version": commit.version,
            "full_total": commit.full_total,
            "pending_jobs": [dict(row) for row in eng.store.resume_jobs(eng.project.project_id)],
        })

    @app.route("/healthz")
    def healthz():
        return jsonify({"status": "ok", "db": db_path})

    return app


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    project_path = os.path.join(os.path.dirname(here), "data", "packaging_project.json")
    app = create_app(project_path)
    app.run(host="127.0.0.1", port=5050, debug=False)


if __name__ == "__main__":
    main()
