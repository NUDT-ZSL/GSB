"""End-to-end validation/packaging pipeline and deterministic report model."""

from __future__ import annotations

import concurrent.futures
import os
import random
import struct
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import MASTER_SAMPLE_RATE, VIDEO_FPS
from .container import (
    TYPE_AUDIO, TYPE_SUBTITLE, IndexEntry,
    encode_subtitle_block, language_code, write_master,
)
from .errors import MvstError
from .manifest import Manifest, load_manifest
from .resampler import resample_track
from .subtitles import parse_srt, snap_and_resolve
from .timebase import (
    frame_to_master_sample, ms_to_master_sample,
    source_samples_to_master_samples,
)
from .wavfile import parse_wav

# Fixed, documented top-level key order for every emitted report.
REPORT_KEY_ORDER = (
    "generated_at",
    "conclusion",
    "sources",
    "intermediate",
    "audio",
    "subtitles",
    "warnings",
    "reproducibility",
)


def _fraction(value: Fraction) -> dict[str, int]:
    """Exact rational serialization (never a float)."""
    return {"numerator": value.numerator, "denominator": value.denominator}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_video(root: Path, manifest: Manifest) -> tuple[bytes, int]:
    if not manifest.video_file:
        raise MvstError("E_MANIFEST", pointer="/video_file")
    path = root / manifest.video_file
    try:
        blob = path.read_bytes()
    except OSError:
        raise MvstError("E_VIDEO_MISSING", file=str(path))
    # Raw video frames: fixed 3840-byte frames generated deterministically.
    frame_size = 3840
    if frame_size <= 0 or len(blob) % frame_size != 0:
        raise MvstError("E_VIDEO_INVALID", file=str(path))
    return blob, len(blob) // frame_size


def _audio_worker(
    track_index: int,
    language: str,
    file_path: str,
    offset_ms: int,
    gain_db: float,
) -> dict[str, Any]:
    """One independent worker: parses its WAV and owns every writable buffer."""
    try:
        wav = parse_wav(file_path)
        result = resample_track(
            track_index, wav.samples, wav.sample_rate, gain_db,
        )
        exact = source_samples_to_master_samples(
            wav.source_samples, wav.sample_rate
        )
        start_sample = ms_to_master_sample(offset_ms)
        return {
            "ok": True,
            "index": track_index,
            "language": language,
            "file": file_path,
            "source_rate": wav.sample_rate,
            "source_samples": wav.source_samples,
            "duration_sec": _fraction(wav.duration),
            "offset_ms": offset_ms,
            "start_sample": start_sample,
            "gain_db": gain_db,
            "resampled_samples": result.resampled_samples,
            "exact_master_samples": _fraction(exact),
            "clip_count": result.clip_count,
            "peak": result.peak,
            "rms": result.rms,
            "length_delta": result.length_delta,
            "reproducible": result.reproducible,
            "samples": result.samples,
            "sample_range": (start_sample,
                             start_sample + result.resampled_samples - 1),
            "warnings": result.clip_count > 0,
        }
    except MvstError as exc:
        return {"ok": False, "index": track_index, "language": language,
                "file": file_path, "error_code": exc.error_code,
                "fields": exc.fields}
    except Exception:  # noqa: BLE001 - worker boundary, surfaced as E_RESAMPLE
        return {"ok": False, "index": track_index, "language": language,
                "file": file_path, "error_code": "E_RESAMPLE",
                "fields": {"track": track_index}}


def _subtitle_worker(file_path: str, track_index: int) -> dict[str, Any]:
    try:
        raw = parse_srt(file_path)
        result = snap_and_resolve(raw, file_path)
        return {"ok": True, "index": track_index, "file": file_path, "result": result}
    except MvstError as exc:
        return {"ok": False, "index": track_index, "file": file_path,
                "error_code": exc.error_code, "fields": exc.fields}
    except Exception:  # noqa: BLE001
        return {"ok": False, "index": track_index, "file": file_path,
                "error_code": "E_SUB_FORMAT", "fields": {"file": file_path}}


def _check_language_consistency(manifest: Manifest) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for track in manifest.tracks:
        if track.language in seen:
            raise MvstError("E_LANG_DUP", language=track.language,
                            track=track.index)
        seen.add(track.language)

    sub_languages = {s.language for s in manifest.subtitles}
    audio_languages = {t.language for t in manifest.tracks}
    for language in manifest.languages:
        has_audio = language in audio_languages
        has_sub = language in sub_languages
        if not has_audio and not has_sub:
            raise MvstError("E_LANG_MISSING", language=language)
        if has_audio != has_sub:
            warnings.append({
                "error_code": "E_LANG_PARTIAL",
                "language": language,
                "has_audio": has_audio,
                "has_subtitle": has_sub,
            })
    return warnings


def _sub_cue_texts(path: str) -> dict[int, str]:
    texts: dict[int, str] = {}
    for cue in parse_srt(path):
        texts[cue.seq] = " ".join(cue.text)
    return texts


def run_pipeline(
    manifest_path: str | Path,
    root: str | Path,
    jobs: int,
    *,
    package_out: str | Path | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], int]:
    """Execute validate/package.  Returns (report, exit_code)."""
    manifest = load_manifest(manifest_path)
    root = Path(root)
    cpu = os.cpu_count() or 1
    workers = max(1, min(jobs, cpu))

    language_warnings = _check_language_consistency(manifest)

    # Video raw frame data.
    video_blob, video_frames = _read_video(root, manifest)

    # Build the job list.  The shuffle env var intentionally perturbs internal
    # mapping iteration / submission order; outputs remain identical because
    # results are merged back by track index.
    jobs_list: list[tuple[str, tuple]] = []
    for track in manifest.tracks:
        jobs_list.append(("audio", (
            track.index, track.language, str(root / track.file),
            track.offset_ms, track.gain_db,
        )))
    for sub in manifest.subtitles:
        jobs_list.append(("subtitle", (str(root / sub.file), sub.index)))
    if os.environ.get("MVST_SHUFFLE") == "1":
        random.Random(20261113).shuffle(jobs_list)

    audio_results: dict[int, dict[str, Any]] = {}
    sub_results: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for kind, args in jobs_list:
            if kind == "audio":
                futures.append(pool.submit(_audio_worker, *args))
            else:
                futures.append(pool.submit(_subtitle_worker, *args))
        for future in concurrent.futures.as_completed(futures):
            payload = future.result()
            if "result" in payload or payload.get("error_code", "").startswith("E_SUB"):
                sub_results[payload["index"]] = payload
            else:
                audio_results[payload["index"]] = payload

    warnings: list[dict[str, Any]] = list(language_warnings)
    failures: list[dict[str, Any]] = []

    # ---- audio section, merged in stable track-index order ----
    audio_section: list[dict[str, Any]] = []
    audio_payloads: list[tuple[IndexEntry, bytes]] = []
    for track in manifest.tracks:
        res = audio_results[track.index]
        if not res["ok"]:
            failures.append({"track": track.index,
                             "error_code": res["error_code"], **res["fields"]})
            continue
        if res["warnings"]:
            warnings.append({
                "error_code": "E_CLIP_RISK",
                "track": track.index,
                "language": track.language,
                "clip_count": res["clip_count"],
            })
        entry = IndexEntry(
            track_type=TYPE_AUDIO,
            track_number=track.index,
            language_code=language_code(track.language),
            offset_ms=track.offset_ms,
            sample_rate=MASTER_SAMPLE_RATE,
            byte_offset=0,
            start_unit=res["sample_range"][0],
            end_unit=res["sample_range"][1],
            data_length=len(res["samples"]) * 2,
        )
        audio_payloads.append((entry, struct.pack("<%dh" % len(res["samples"]),
                                                  *res["samples"])))
        audio_section.append({k: res[k] for k in (
            "index", "language", "file", "source_rate", "source_samples",
            "duration_sec", "offset_ms", "start_sample", "gain_db",
            "resampled_samples", "exact_master_samples", "clip_count",
            "peak", "rms", "length_delta", "reproducible", "sample_range",
        )})

    # ---- subtitle section ----
    sub_section: list[dict[str, Any]] = []
    subtitle_payloads: list[tuple[IndexEntry, bytes]] = []
    for sub in manifest.subtitles:
        res = sub_results[sub.index]
        if not res["ok"]:
            failures.append({"track": sub.index,
                             "error_code": res["error_code"], **res["fields"]})
            continue
        result = res["result"]
        warnings.extend(result.format_errors)
        warnings.extend(result.warnings)
        texts = _sub_cue_texts(res["file"])
        cue_payload = [(cue.seq, cue.new_start_frame, cue.new_end_frame,
                        texts.get(cue.seq, ""))
                       for cue in result.cues if not cue.dropped]
        block = encode_subtitle_block(cue_payload)
        if result.accepted:
            start_frame = min(c[1] for c in result.accepted)
            end_frame = max(c[2] for c in result.accepted)
        else:
            start_frame, end_frame = 0, -1
        entry = IndexEntry(
            track_type=TYPE_SUBTITLE,
            track_number=sub.index,
            language_code=language_code(sub.language),
            offset_ms=0,
            sample_rate=VIDEO_FPS,
            byte_offset=0,
            start_unit=start_frame,
            end_unit=end_frame,
            data_length=len(block),
        )
        subtitle_payloads.append((entry, block))
        sub_section.append({
            "index": sub.index,
            "language": sub.language,
            "file": res["file"],
            "adjusted": [cue.adjusted for cue in result.cues
                         if cue.raw_start_frame is not None
                         and not cue.dropped],
            "cues": [
                {
                    "seq": cue.seq,
                    "raw_start_frame": cue.raw_start_frame,
                    "raw_end_frame": cue.raw_end_frame,
                    "new_start_frame": cue.new_start_frame,
                    "new_end_frame": cue.new_end_frame,
                    "adjusted": cue.adjusted,
                    "dropped": cue.dropped,
                }
                for cue in result.cues
            ],
        })

    # ---- shared intermediate values (single numeric source) ----
    # Subtitle ms->frame snapshots feed the same rational helper used by the
    # snapping stage, so the report's intermediate section can be compared
    # item-by-item against the expected frame numbers.
    subtitle_snaps: list[dict[str, Any]] = []
    for sub in manifest.subtitles:
        res = sub_results.get(sub.index)
        if res is None or not res["ok"]:
            continue
        for cue in res["result"].cues:
            if cue.raw_start_frame is None:
                continue
            subtitle_snaps.append({
                "track": sub.index,
                "language": sub.language,
                "seq": cue.seq,
                "start_frame": cue.raw_start_frame,
                "end_frame": cue.raw_end_frame,
            })
    intermediate = {
        "master_sample_rate": MASTER_SAMPLE_RATE,
        "video_fps": VIDEO_FPS,
        "frame_duration_master_samples": MASTER_SAMPLE_RATE // VIDEO_FPS,
        "audio_offsets": [
            {"track": t.index, "language": t.language, "offset_ms": t.offset_ms,
             "start_sample": ms_to_master_sample(t.offset_ms)}
            for t in manifest.tracks
        ],
        "subtitle_frame_snaps": subtitle_snaps,
        "video_frame_count": video_frames,
        "video_sample_range": [
            0, frame_to_master_sample(video_frames) - 1
        ],
    }

    reproducibility = {
        "reproducible": all(a["reproducible"] for a in audio_section)
        if audio_section else True,
        "resampler": "windowed_sinc",
        "length_deltas": [
            {"track": a["index"], "delta": a["length_delta"],
             "source": "exact rational length rounded to nearest sample"}
            for a in audio_section
        ],
    }

    report: dict[str, Any] = {
        "generated_at": _now(),
        "conclusion": "FAIL" if failures else "PASS",
        "sources": {
            "manifest": str(manifest_path),
            "root": str(root),
            "master_sample_rate": MASTER_SAMPLE_RATE,
            "video_fps": VIDEO_FPS,
            "video": {"file": manifest.video_file, "frames": video_frames,
                      "bytes": len(video_blob)},
            "audio_tracks": len(manifest.tracks),
            "subtitle_tracks": len(manifest.subtitles),
            "jobs": workers,
        },
        "intermediate": intermediate,
        "audio": audio_section,
        "subtitles": sub_section,
        "warnings": warnings,
        "reproducibility": reproducibility,
    }

    if failures:
        # Business failure after all workers have been collected.  The full
        # report (including successful tracks' statistics) rides on the error.
        first = failures[0]
        raise MvstError(
            first["error_code"],
            report=report,
            **{k: v for k, v in first.items() if k != "error_code"},
        )

    if package_out is not None:
        out_path = Path(package_out)
        if out_path.exists() and not force:
            raise MvstError("E_TARGET_EXISTS", file=str(out_path))
        write_master(
            out_path,
            video_frames=video_frames,
            video_bytes=video_blob,
            audio_payloads=audio_payloads,
            subtitle_payloads=subtitle_payloads,
        )

    return report, 0
