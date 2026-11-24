"""Atomic group commit of audio deliverables and the measurement report."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .errors import PipelineError
from .util import canonical_json_bytes
from .wavio import inspect_wav


def read_existing_state(report_path: Path,
                        wav_paths: Mapping[str, Path]) -> dict[str, object]:
    version: str | None = None
    frames: dict[str, int] = {}
    if report_path.exists():
        try:
            with report_path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
            if isinstance(report, dict):
                value = report.get("output_version")
                if isinstance(value, str):
                    version = value
        except (OSError, UnicodeError, json.JSONDecodeError):
            version = None
    for input_id, path in wav_paths.items():
        if path.exists():
            try:
                frames[input_id] = inspect_wav(path).frames
            except PipelineError:
                frames[input_id] = -1
    return {"version": version, "frames": frames}


def _failure_point() -> str | None:
    value = os.environ.get("AUDIO_PIPELINE_FAIL_COMMIT")
    return value.strip().lower() if value else None


def commit_group(
    output_dir: Path,
    *,
    wav_payloads: Mapping[str, bytes],
    expected_frames: Mapping[str, int],
    report_bytes: bytes,
    version: str,
    existing: dict[str, object],
    force: bool = False,
) -> dict[str, object]:
    """Replace a WAV set and report as one rollback-capable transaction."""
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    final_paths = {
        input_id: output_dir / f"{input_id}.wav"
        for input_id in sorted(wav_payloads)
    }
    stale_wavs = {
        path for path in output_dir.glob("*.wav")
        if path.resolve() not in {value.resolve() for value in final_paths.values()}
    }
    old_version = existing.get("version")
    old_frames = existing.get("frames", {})
    same_version = old_version == version
    frames_match = (
        isinstance(old_frames, dict)
        and all(old_frames.get(input_id) == frames
                for input_id, frames in expected_frames.items())
    )
    if (
        same_version and frames_match and report_path.exists()
        and not stale_wavs and not force
    ):
        return {
            "committed": False,
            "skipped": True,
            "reason": "same_version",
            "version": version,
            "previous_version": old_version,
            "previous_frames": old_frames,
        }

    failure = _failure_point()
    if failure == "write":
        raise PipelineError("E_IO", "injected failure while staging outputs")

    stamp = f"{os.getpid()}.{id(report_bytes)}"
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    try:
        for input_id, payload in sorted(wav_payloads.items()):
            final = final_paths[input_id]
            temp = final.with_name(f".{final.name}.{stamp}.tmp")
            with temp.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged[final] = temp
        report_temp = report_path.with_name(f".{report_path.name}.{stamp}.tmp")
        with report_temp.open("wb") as handle:
            handle.write(report_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        staged[report_path] = report_temp

        backup_targets = list(staged) + sorted(stale_wavs)
        for index, final in enumerate(backup_targets):
            if final.exists():
                backup = final.with_name(f".{final.name}.{stamp}.{index}.bak")
                os.replace(final, backup)
                backups[final] = backup
        for final, temp in staged.items():
            if final == report_path and failure == "after_audio":
                raise OSError("injected failure before report replacement")
            if final == report_path and failure == "report":
                raise OSError("injected failure while replacing report")
            os.replace(temp, final)
            installed.append(final)
        for backup in backups.values():
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass
        return {
            "committed": True,
            "skipped": False,
            "version": version,
            "previous_version": old_version,
            "previous_frames": old_frames,
        }
    except OSError as exc:
        for final in installed:
            try:
                final.unlink(missing_ok=True)
            except OSError:
                pass
        for final, backup in backups.items():
            try:
                if backup.exists():
                    os.replace(backup, final)
            except OSError:
                pass
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        raise PipelineError("E_IO", f"atomic output commit failed: {exc}") from exc
    finally:
        for temp in staged.values():
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def serialize_report(report: dict[str, object]) -> bytes:
    return canonical_json_bytes(report) + b"\n"
