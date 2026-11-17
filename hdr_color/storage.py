"""Same-directory atomic JSON writes and output metadata inspection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .errors import BusinessError


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_existing_output_metadata(output_dir: Path) -> dict[str, Any] | None:
    """Read frame count, parameter digest and version from completed outputs."""

    frames_path = output_dir / "converted_frames.json"
    report_path = output_dir / "statistics_report.json"
    if not frames_path.is_file() or not report_path.is_file():
        return None
    try:
        frames = read_json_file(frames_path)
        report = read_json_file(report_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(frames, dict) or not isinstance(report, dict):
        return None
    if frames.get("status") != "complete" or report.get("status") != "complete":
        return None
    version = frames.get("version")
    if not isinstance(version, str) or report.get("version") != version:
        return None
    frame_count = frames.get("frames_count")
    if frame_count != report.get("frames_count"):
        return None
    parameter_digest = frames.get("parameter_digest")
    if not isinstance(parameter_digest, str):
        return None
    if report.get("parameter_digest") != parameter_digest:
        return None
    if frames.get("generation_id") != report.get("generation_id"):
        return None
    return {
        "version": version,
        "parameter_digest": parameter_digest,
        "frames_count": frame_count,
        "generation_id": frames.get("generation_id"),
        "report_generation_id": report.get("generation_id"),
    }


def ensure_writable_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            prefix=".write-check-", suffix=".tmp", dir=str(path)
        )
        os.close(descriptor)
        Path(name).unlink()
    except OSError as exc:
        raise BusinessError(
            "E_OUTPUT_DIR",
            "output directory is not writable",
            {"path": str(path), "error": str(exc)},
        ) from exc


def remove_tree_files(directory: Path) -> None:
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        try:
            if entry.is_file() or entry.is_symlink():
                entry.unlink()
        except OSError:
            pass
    try:
        directory.rmdir()
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
