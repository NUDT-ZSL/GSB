"""Trace serialization and exact frame-by-frame comparison."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import AquaError
from .model import Project
from .scheduler import Scheduler


TRACE_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def project_hash(project: Project) -> str:
    return hashlib.sha256(canonical_json(project.to_dict()).encode("utf-8")).hexdigest()


def build_trace_document(
    project: Project,
    scheduler: Scheduler,
    stats: dict[str, Any],
) -> dict[str, Any]:
    return {
        "trace_version": TRACE_VERSION,
        "tool": "aqua-edge",
        "project_hash": project_hash(project),
        "project": project.to_dict(),
        "policy": {
            "max_concurrent_streams": project.network.max_concurrent_streams,
            "priorities": {stream.stream_id: stream.priority for stream in project.streams},
            "windows": {stream.stream_id: stream.window_bytes for stream in project.streams},
        },
        "events": {
            "consumer_rate_events": [
                event.to_dict() for event in project.consumer.events
            ],
            "outages": [outage.to_dict() for outage in project.outages],
        },
        "arrival_sequences": {
            stream.stream_id: [frame.to_dict() for frame in stream.frames]
            for stream in project.streams
        },
        "summary": stats,
        "frames": scheduler.frame_traces(),
    }


def save_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def load_json(path: str | Path) -> Any:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise AquaError("E_IO", "文件不存在", {"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise AquaError(
            "E_JSON",
            "文件不是合法 JSON",
            {"path": str(path), "line": exc.lineno, "column": exc.colno},
        ) from exc


def compare_frame_traces(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
    *,
    tolerance_ms: float = 1e-9,
) -> dict[str, Any]:
    left_by_id = {record["frame_id"]: record for record in left}
    right_by_id = {record["frame_id"]: record for record in right}
    all_ids = sorted(set(left_by_id).union(right_by_id))
    differences: list[str] = []
    for frame_id in all_ids:
        a = left_by_id.get(frame_id)
        b = right_by_id.get(frame_id)
        if a is None or b is None:
            differences.append(frame_id)
            continue
        if a.get("status") != b.get("status"):
            differences.append(frame_id)
            continue
        for field in ("logical_time_ms", "start_ms", "end_ms", "queue_delay_ms"):
            av = a.get(field)
            bv = b.get(field)
            if av is None or bv is None:
                if av != bv:
                    differences.append(frame_id)
                    break
            elif abs(float(av) - float(bv)) > tolerance_ms:
                differences.append(frame_id)
                break
    return {
        "identical": not differences,
        "compared_frames": len(all_ids),
        "different_frames": len(differences),
        "first_difference": differences[0] if differences else None,
        "difference_frame_ids": differences[:50],
    }

