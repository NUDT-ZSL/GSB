"""Local Flask service for segment, snapshot, replay, and report access."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file

from .io_utils import read_json
from .snapshots import (
    SegmentNotFound,
    SnapshotError,
    SnapshotNotFound,
    SnapshotStore,
)


def _json_error(error: SnapshotError):
    response = jsonify(error.as_dict())
    response.status_code = error.http_status
    response.headers["X-Business-Code"] = str(error.business_code)
    return response


def create_app(data_dir: str | Path | None = None) -> Flask:
    app = Flask(__name__)
    root = Path(
        data_dir
        or os.environ.get("WELD_DATA_DIR")
        or Path(__file__).resolve().parent.parent / "data"
    ).resolve()
    project_path = root / "project.json"
    project = read_json(project_path) if project_path.exists() else None
    layout = project["speaker_layout"] if project else None
    store = SnapshotStore(root / "snapshots", layout)
    segment_registry = {
        item["segment_id"]: item for item in project["segments"]
    } if project else {}

    def segment_metadata(segment_id: str) -> dict:
        if segment_id not in segment_registry:
            raise SegmentNotFound(
                "training segment does not exist",
                segment_id=segment_id,
            )
        return segment_registry[segment_id]

    def active_snapshot(segment_id: str):
        segment_metadata(segment_id)
        return store.active(segment_id)

    def version_descriptor(record: dict | None) -> dict | None:
        if record is None:
            return None
        return {
            "version": record["version"],
            "status": record["status"],
            "content_sha256": record["content_sha256"],
        }

    @app.get("/")
    def index():
        return jsonify(
            {
                "ok": True,
                "service": "welding-listening-training",
                "bind_policy": "loopback by default (127.0.0.1)",
                "endpoints": [
                    "GET /api/health",
                    "GET /api/segments",
                    "GET /api/segments/<segment_id>",
                    "GET /api/segments/<segment_id>/versions",
                    "GET /api/segments/<segment_id>/replay?version=vNNNN",
                    "GET /api/segments/<segment_id>/report?version=vNNNN",
                    "GET /api/segments/<segment_id>/consistency?version=vNNNN",
                    "GET /api/snapshots/<segment_id>/<version>/replay",
                    "GET /api/snapshots/<segment_id>/<version>/audio",
                    "GET /api/validation",
                ],
            }
        )

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "project_loaded": project is not None,
                "data_dir": str(root),
                "segments": len(segment_registry),
            }
        )

    @app.get("/api/segments")
    def list_segments():
        items = []
        for segment_id, metadata in segment_registry.items():
            manifest = store.list_versions(segment_id)
            active_record = next(
                (
                    record
                    for record in manifest["versions"]
                    if record["version"] == manifest.get("active_version")
                ),
                None,
            )
            items.append(
                {
                    "segment_id": segment_id,
                    "boundary_type": metadata["boundary_type"],
                    "description": metadata["description"],
                    "source_file": metadata["source_file"],
                    "active_snapshot": version_descriptor(active_record),
                    "snapshot_version_count": len(manifest["versions"]),
                    "replay_url": (
                        f"/api/segments/{segment_id}/replay?version="
                        f"{active_record['version']}"
                        if active_record
                        else None
                    ),
                    "report_url": (
                        f"/api/segments/{segment_id}/report?version="
                        f"{active_record['version']}"
                        if active_record
                        else None
                    ),
                }
            )
        return jsonify({"ok": True, "segments": items})

    @app.get("/api/segments/<segment_id>")
    def segment_detail(segment_id: str):
        metadata = segment_metadata(segment_id)
        segment_path = root / metadata["source_file"]
        if not segment_path.exists():
            raise SegmentNotFound(
                "training segment source file does not exist",
                segment_id=segment_id,
            )
        segment = read_json(segment_path)
        active = active_snapshot(segment_id)
        return jsonify(
            {
                "ok": True,
                "segment": {
                    "segment_id": segment_id,
                    "boundary_type": metadata["boundary_type"],
                    "description": metadata["description"],
                    "seed": segment["seed"],
                    "duration_s": segment["duration_s"],
                    "frame_rate_hz": segment["frame_rate_hz"],
                    "frame_count": segment["frame_count"],
                    "source_digest": segment.get("source_digest"),
                    "state_intervals": segment["state_intervals"],
                    "process_fields": list(segment["process"].keys()),
                    "pool_fields": list(segment["pool"].keys()),
                    "spatter_event_count": len(segment["spatter_events"]),
                },
                "active_snapshot": version_descriptor(active),
            }
        )

    @app.get("/api/segments/<segment_id>/versions")
    def segment_versions(segment_id: str):
        segment_metadata(segment_id)
        manifest = store.list_versions(segment_id)
        return jsonify(
            {
                "ok": True,
                "segment_id": segment_id,
                "active_version": manifest["active_version"],
                "versions": manifest["versions"],
            }
        )

    def replay_response(segment_id: str, version: str):
        segment_metadata(segment_id)
        snapshot = store.load(segment_id, version)
        audio = snapshot.get("audio", {})
        from .validation import compare_realtime_and_replay

        replay_verification = compare_realtime_and_replay(
            segment_id=segment_id,
            snapshot_version=version,
            realtime_frames=snapshot["field"]["frames"],
            replay_frames=store.replay_frames(snapshot),
        )
        audio_url = (
            f"/api/snapshots/{segment_id}/{version}/audio"
            if audio.get("path")
            else None
        )
        return jsonify(
            {
                "ok": True,
                "segment_id": segment_id,
                "version": version,
                "status": "active",
                "content_sha256": snapshot["_manifest_record"][
                    "content_sha256"
                ],
                "segment": snapshot["segment"],
                "coordinate_system": snapshot["coordinate_system"],
                "speaker_layout": snapshot["speaker_layout"],
                "source_tables": snapshot["source_tables"],
                "field": snapshot["field"],
                "audio": {**audio, "url": audio_url},
                "replay_verification": {
                    "frame_count": replay_verification["frame_count"],
                    "max_abs_differences": replay_verification[
                        "max_abs_differences"
                    ],
                    "pass": replay_verification["pass"],
                },
                "report_url": (
                    f"/api/segments/{segment_id}/report?version={version}"
                ),
                "consistency_url": (
                    f"/api/segments/{segment_id}/consistency?version={version}"
                ),
            }
        )

    @app.get("/api/snapshots/<segment_id>/<version>/replay")
    def snapshot_replay(segment_id: str, version: str):
        return replay_response(segment_id, version)

    @app.get("/api/segments/<segment_id>/replay")
    def segment_replay(segment_id: str):
        version = request.args.get("version")
        if version is None:
            from .snapshots import SnapshotVersionRequired

            raise SnapshotVersionRequired(
                "replay requires a snapshot version query parameter",
                segment_id=segment_id,
            )
        return replay_response(segment_id, version)

    @app.get("/api/snapshots/<segment_id>/<version>/audio")
    def snapshot_audio(segment_id: str, version: str):
        segment_metadata(segment_id)
        snapshot = store.load(segment_id, version)
        audio = snapshot.get("audio", {})
        audio_path = (root / audio.get("path", "")).resolve()
        if not audio_path.exists():
            error = SnapshotError(
                "snapshot audio artifact is missing",
                **{"segment_id": segment_id, "requested_version": version},
            )
            error.code = "SNAPSHOT_AUDIO_MISSING"
            error.business_code = 2005
            error.http_status = 404
            raise error
        return send_file(
            audio_path,
            mimetype="audio/wav",
            as_attachment=False,
            conditional=True,
        )

    @app.get("/api/segments/<segment_id>/report")
    def segment_report(segment_id: str):
        segment_metadata(segment_id)
        manifest = store.list_versions(segment_id)
        version = request.args.get("version") or manifest.get("active_version")
        if not version:
            raise SnapshotNotFound(
                "segment has no active snapshot report",
                segment_id=segment_id,
            )
        store.load(segment_id, version)
        report_path = root / "reports" / segment_id / f"{version}.json"
        if not report_path.exists():
            error = SnapshotError(
                "report artifact is missing for this snapshot version",
                segment_id=segment_id,
                requested_version=version,
            )
            error.code = "REPORT_NOT_FOUND"
            error.business_code = 3001
            error.http_status = 404
            raise error
        return jsonify(
            {
                "ok": True,
                "segment_id": segment_id,
                "version": version,
                "report": read_json(report_path),
            }
        )

    @app.get("/api/segments/<segment_id>/consistency")
    def segment_consistency(segment_id: str):
        segment_metadata(segment_id)
        manifest = store.list_versions(segment_id)
        version = request.args.get("version") or manifest.get("active_version")
        if not version:
            raise SnapshotNotFound(
                "segment has no active consistency record",
                segment_id=segment_id,
            )
        store.load(segment_id, version)
        path = root / "outputs" / segment_id / "consistency.json"
        if not path.exists():
            raise SnapshotNotFound(
                "consistency record is missing",
                segment_id=segment_id,
                requested_version=version,
            )
        return jsonify(
            {
                "ok": True,
                "segment_id": segment_id,
                "version": version,
                "consistency": read_json(path),
            }
        )

    @app.get("/api/validation")
    def validation():
        path = root / "validation" / "records.json"
        if not path.exists():
            raise SnapshotNotFound("validation record is missing")
        return jsonify({"ok": True, "validation": read_json(path)})

    @app.errorhandler(SnapshotError)
    def snapshot_error_handler(error: SnapshotError):
        return _json_error(error)

    @app.errorhandler(404)
    def not_found(_error):
        response = jsonify(
            {
                "ok": False,
                "code": "ROUTE_NOT_FOUND",
                "business_code": 9001,
                "error": {
                    "code": "ROUTE_NOT_FOUND",
                    "business_code": 9001,
                    "message": "route not found",
                },
            }
        )
        response.status_code = 404
        response.headers["X-Business-Code"] = "9001"
        return response

    return app
