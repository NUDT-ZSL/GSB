"""Fixed player keyframe export and internal per-frame curve artifacts."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any

import numpy as np

from .catalog import CHANNEL_LABELS
from .io_utils import clean_float, json_bytes, write_json, write_npz


def _frame_rows(curves: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    internal_rows: list[dict[str, Any]] = []
    keyframe_rows: list[dict[str, Any]] = []
    for frame in range(curves.frame_count):
        time_value = round(frame / curves.fps, 9)
        values = [
            clean_float(curves.values[frame, index])
            for index in range(len(curves.channels))
        ]
        internal_rows.append(
            {
                "frame": frame,
                "time": time_value,
                "values": values,
            }
        )
        keyframe_rows.append(
            {
                "frame": frame,
                "time": time_value,
                "values": list(values),
            }
        )
    return internal_rows, keyframe_rows


def build_export_payloads(curves: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    internal_rows, keyframe_rows = _frame_rows(curves)
    digest = hashlib.sha256()
    digest.update(
        json_bytes(
            {
                "fps": curves.fps,
                "frame_count": curves.frame_count,
                "channels": list(curves.channels),
            }
        )
    )
    digest.update(np.asarray(curves.values, dtype="<f8").tobytes())
    curve_hash = digest.hexdigest()
    curve_payload = {
        "format": "museum-face-curves/v1",
        "fps": curves.fps,
        "frame_count": curves.frame_count,
        "duration_seconds": round(curves.frame_count / curves.fps, 9),
        "channels": list(curves.channels),
        "frames": internal_rows,
        "curve_sha256": curve_hash,
    }
    keyframe_payload = {
        "format": "museum-face-keyframes/v1",
        "schema_version": 1,
        "fps": curves.fps,
        "frame_count": curves.frame_count,
        "keyframe_count": curves.frame_count,
        "duration_seconds": round(curves.frame_count / curves.fps, 9),
        "channels": list(curves.channels),
        "channel_order": list(curves.channels),
        "channel_labels": {channel: CHANNEL_LABELS[channel] for channel in curves.channels},
        "coordinate_ranges": {
            "jaw_open": [0.0, 1.0],
            "lip_width": [0.0, 1.0],
            "mouth_corner": [-1.0, 1.0],
            "brow_raise": [-1.0, 1.0],
            "brow_inner_up": [-1.0, 1.0],
        },
        "keyframes": keyframe_rows,
        "curve_sha256": curve_hash,
    }
    if [
        (row["frame"], row["time"], row["values"]) for row in keyframe_rows
    ] != [
        (row["frame"], row["time"], row["values"]) for row in internal_rows
    ]:
        raise RuntimeError("keyframe payload diverged from internal curve payload")
    return curve_payload, keyframe_payload


def write_curve_artifacts(
    directory: Path,
    curves: Any,
    *,
    prefix: str = "",
) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    curve_payload, keyframe_payload = build_export_payloads(curves)
    curve_path = directory / f"{prefix}channel_curves.json"
    keyframe_path = directory / f"{prefix}face_animation.json"
    csv_path = directory / f"{prefix}channel_curves.csv"
    npz_path = directory / f"{prefix}channel_curves.npz"
    write_json(curve_path, curve_payload)
    write_json(keyframe_path, keyframe_payload)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["frame", "time_seconds", *list(curves.channels)])
        for frame in range(curves.frame_count):
            writer.writerow(
                [
                    frame,
                    f"{frame / curves.fps:.9f}",
                    *[
                        f"{curves.values[frame, index]:.9f}"
                        for index in range(len(curves.channels))
                    ],
                ]
            )
    write_npz(
        npz_path,
        fps=np.asarray(curves.fps, dtype="<f8"),
        frame_count=np.asarray(curves.frame_count, dtype="<i8"),
        channels=np.asarray(list(curves.channels), dtype="U32"),
        frame_times_us=np.asarray(curves.frame_times_us, dtype="<i8"),
        curves=np.asarray(curves.values, dtype="<f8"),
    )
    return {
        "curves": str(curve_path),
        "keyframes": str(keyframe_path),
        "csv": str(csv_path),
        "npz": str(npz_path),
    }


def keyframe_sha256(payload: dict[str, Any]) -> str:
    digest = hashlib.sha256()
    digest.update(
        json_bytes(
            {
                "format": payload["format"],
                "fps": payload["fps"],
                "frame_count": payload["frame_count"],
                "channel_order": payload["channel_order"],
            }
        )
    )
    for keyframe in payload["keyframes"]:
        digest.update(
            json_bytes(
                {
                    "frame": keyframe["frame"],
                    "time": keyframe["time"],
                    "values": keyframe["values"],
                }
            )
        )
    return digest.hexdigest()
