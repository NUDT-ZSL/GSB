"""End-to-end mixing pipeline.

All timeline coordinates are target-rate frame indices.  Per track the flow
is: decode PCM -> resample to target rate -> crop -> place at offset ->
measure post-gain peak -> sum.  The soft limiter runs once on the final sum.
"""

import json
import os

import numpy as np
import soundfile as sf

from . import atomic
from .errors import (
    E_EMPTY,
    E_FORMAT,
    E_MISSING,
    E_OK,
    E_WRITE,
    describe,
)
from .manifest import ManifestError, load_manifest
from .mixer import apply_gain, peak_level, soft_limit
from .pcm import TrackFormatError, read_wav_mono
from .resample import expected_output_frames, resample

SUBTYPE = {16: "PCM_16", 24: "PCM_24"}


def _empty_track_report(index, code, reason):
    return {
        "track_index": index,
        "status": code,
        "error": reason,
        "input_frames": None,
        "converted_frames": None,
        "start_frame": None,
        "end_frame": None,
        "input_clip": None,
        "post_gain_peak": None,
    }


def _read_existing_version(path):
    """Read frame count and sample rate of an existing output WAV."""
    if not os.path.isfile(path):
        return None
    try:
        info = sf.info(path)
        return {"frames": info.frames, "sample_rate": info.samplerate}
    except Exception as exc:  # unreadable old file is treated as no version
        return {"error": str(exc)}


def process(manifest_path):
    """Run the full pipeline. Returns ``(report_dict, exit_code)``."""
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        report = {
            "status": exc.code,
            "error": exc.reason,
            "target_sample_rate": None,
            "target_bit_depth": None,
            "output_frames": 0,
            "mix_peak_pre_limit": None,
            "mix_peak_final": None,
            "output_replaced": None,
            "tracks": [],
        }
        return report, exc.code

    target_rate = manifest["target_sample_rate"]
    target_bits = manifest["target_bit_depth"]
    track_reports = []
    placements = []
    any_failure = False

    for spec in manifest["tracks"]:
        index = spec["index"]
        if not os.path.isfile(spec["file"]):
            any_failure = True
            track_reports.append(
                _empty_track_report(
                    index, E_MISSING, f"track file missing: {spec['file']}"
                )
            )
            continue
        try:
            samples, fs_in, bit_depth, input_clip = read_wav_mono(spec["file"])
        except TrackFormatError as exc:
            any_failure = True
            track_reports.append(_empty_track_report(index, E_FORMAT, exc.reason))
            continue

        input_frames = int(samples.shape[0])
        converted = resample(samples, fs_in, target_rate)
        converted_frames = int(converted.shape[0])
        expected = expected_output_frames(input_frames, fs_in, target_rate)
        if converted_frames != expected:
            any_failure = True
            track_reports.append(
                _empty_track_report(
                    index,
                    E_FORMAT,
                    f"resample length {converted_frames} != expected {expected}",
                )
            )
            continue

        crop_start = spec["crop_start"]
        crop_end = (
            converted_frames
            if spec["crop_end"] is None
            else min(spec["crop_end"], converted_frames)
        )
        if crop_start >= crop_end:
            track_reports.append(
                {
                    "track_index": index,
                    "status": E_EMPTY,
                    "error": "crop interval contains no samples",
                    "input_frames": input_frames,
                    "converted_frames": converted_frames,
                    "start_frame": None,
                    "end_frame": None,
                    "input_clip": input_clip,
                    "post_gain_peak": 0.0,
                }
            )
            continue

        segment = converted[crop_start:crop_end]
        segment_gained = apply_gain(segment, spec["gain"])
        start_frame = spec["offset"] + crop_start
        end_frame = start_frame + segment_gained.shape[0]
        if end_frame <= 0:
            track_reports.append(
                {
                    "track_index": index,
                    "status": E_EMPTY,
                    "error": "track lies entirely before frame 0",
                    "input_frames": input_frames,
                    "converted_frames": converted_frames,
                    "start_frame": None,
                    "end_frame": None,
                    "input_clip": input_clip,
                    "post_gain_peak": 0.0,
                }
            )
            continue

        # Drop the part of a negative-offset track that precedes frame 0.
        head_trim = 0
        if start_frame < 0:
            head_trim = -start_frame
            segment_gained = segment_gained[head_trim:]
            start_frame = 0
        end_frame = start_frame + segment_gained.shape[0]

        track_reports.append(
            {
                "track_index": index,
                "status": E_OK,
                "error": None,
                "input_frames": input_frames,
                "converted_frames": converted_frames,
                "start_frame": int(start_frame),
                "end_frame": int(end_frame),
                "input_clip": input_clip,
                "post_gain_peak": peak_level(segment_gained),
            }
        )
        placements.append((start_frame, end_frame, segment_gained))

    if not placements:
        report = {
            "status": E_EMPTY,
            "error": describe(E_EMPTY),
            "target_sample_rate": target_rate,
            "target_bit_depth": target_bits,
            "output_frames": 0,
            "mix_peak_pre_limit": None,
            "mix_peak_final": None,
            "output_replaced": None,
            "tracks": track_reports,
        }
        return report, E_EMPTY

    total_frames = max(end for _, end, _ in placements)
    mix = np.zeros(total_frames, dtype=np.float64)
    for start, end, segment in placements:
        mix[start:end] += segment

    pre_limit_peak = peak_level(mix)
    final_mix = soft_limit(mix)
    final_peak = peak_level(final_mix)

    wav_path = manifest["output"]
    report_path = manifest["report"]
    old_version = _read_existing_version(wav_path)
    version_match = bool(
        old_version
        and "error" not in old_version
        and old_version["frames"] == total_frames
        and old_version["sample_rate"] == target_rate
    )

    try:
        os.makedirs(os.path.dirname(wav_path), exist_ok=True)
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        wav_temp = atomic.temp_path_in(os.path.dirname(wav_path), "mix-")
        sf.write(
            wav_temp,
            final_mix,
            target_rate,
            subtype=SUBTYPE[target_bits],
            format="WAV",
        )
        report_temp = atomic.temp_path_in(os.path.dirname(report_path), "report-")
        status = E_OK if not any_failure else "E_PARTIAL"
        will_replace = not version_match
        report = {
            "status": status,
            "error": None if status == E_OK else "one or more tracks failed",
            "target_sample_rate": target_rate,
            "target_bit_depth": target_bits,
            "output_frames": int(total_frames),
            "mix_peak_pre_limit": pre_limit_peak,
            "mix_peak_final": final_peak,
            "output_version": {
                "frames": int(total_frames),
                "sample_rate": target_rate,
            },
            "previous_version": old_version,
            "output_replaced": will_replace,
            "tracks": track_reports,
        }
        with open(report_temp, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        atomic.commit_pair(
            wav_path, wav_temp, report_path, report_temp, version_match
        )
    except (OSError, sf.LibsndfileError) as exc:
        return _write_failure(track_reports, target_rate, target_bits, exc)

    return report, E_OK if not any_failure else E_FORMAT


def _write_failure(track_reports, target_rate, target_bits, exc):
    report = {
        "status": E_WRITE,
        "error": f"output failure: {exc}",
        "target_sample_rate": target_rate,
        "target_bit_depth": target_bits,
        "output_frames": 0,
        "mix_peak_pre_limit": None,
        "mix_peak_final": None,
        "output_replaced": False,
        "tracks": track_reports,
    }
    return report, E_WRITE
