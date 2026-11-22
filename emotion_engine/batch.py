"""Offline whole-dialog analysis over exact complete frames."""

from __future__ import annotations

import json
import math
import wave
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from .aggregation import build_view, validate_view_structure
from .cache import ArtifactCache
from .config import EngineConfig
from .errors import E_CONFIG_INVALID, EngineError
from .features import PCM_DTYPE, complete_frame_count
from .segments import recount_segment
from .snapshot import SnapshotStore, commit_view
from .util import sha256_json
from .windows import recount_window


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as handle:
        channels = handle.getnchannels()
        sample_width = handle.getsampwidth()
        sample_rate = handle.getframerate()
        frame_count = handle.getnframes()
        raw = handle.readframes(frame_count)
    if channels != 1:
        raise EngineError(
            E_CONFIG_INVALID,
            "WAV must be mono",
            field="wav.channels",
            actual=channels,
        )
    if sample_width != 2:
        raise EngineError(
            E_CONFIG_INVALID,
            "WAV must be 16-bit PCM",
            field="wav.sample_width",
            actual=sample_width,
        )
    pcm = np.frombuffer(raw, dtype=PCM_DTYPE).copy()
    return pcm, sample_rate


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise EngineError(
                    E_CONFIG_INVALID,
                    "invalid JSONL input",
                    field=f"line.{line_number}",
                    error=str(exc),
                ) from exc
    return records


def find_dialog(manifest: Sequence[Mapping[str, Any]], dialog_id: str) -> dict[str, Any]:
    for item in manifest:
        if str(item.get("dialog_id")) == dialog_id:
            return dict(item)
    raise EngineError(
        E_CONFIG_INVALID,
        "dialog_id is not present in the manifest",
        field="dialog_id",
        dialog_id=dialog_id,
    )


def validate_manifest_dialog(entry: Mapping[str, Any], config: EngineConfig) -> None:
    for field in ("dialog_id", "sample_rate", "duration_ms", "chunk_ms", "tier_schema"):
        if field not in entry:
            raise EngineError(
                E_CONFIG_INVALID,
                "manifest entry is missing a required field",
                field=f"manifest.{field}",
            )
    if int(entry["sample_rate"]) <= 0:
        raise EngineError(E_CONFIG_INVALID, "sample_rate must be positive", field="manifest.sample_rate")
    if int(entry["duration_ms"]) <= 0:
        raise EngineError(E_CONFIG_INVALID, "duration_ms must be positive", field="manifest.duration_ms")
    if int(entry["chunk_ms"]) <= 0:
        raise EngineError(E_CONFIG_INVALID, "chunk_ms must be positive", field="manifest.chunk_ms")
    if sha256_json(entry["tier_schema"]) != config.tier_schema_hash():
        raise EngineError(
            E_CONFIG_INVALID,
            "manifest tier_schema does not match the active configuration",
            field="manifest.tier_schema",
            manifest_sha256=sha256_json(entry["tier_schema"]),
            config_sha256=config.tier_schema_hash(),
        )
    speakers = entry.get("speakers")
    if not isinstance(speakers, list) or not speakers:
        raise EngineError(
            E_CONFIG_INVALID,
            "manifest entry must declare a non-empty speakers list",
            field="manifest.speakers",
        )


def truth_alignment(
    frames: Sequence[Mapping[str, Any]],
    truth: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    limit = min(len(frames), len(truth))
    speaker_matches = 0
    speech_matches = 0
    valence_errors: list[float] = []
    arousal_errors: list[float] = []
    for index in range(limit):
        frame = frames[index]
        label = truth[index]
        if int(frame["frame_index"]) != int(label["frame_index"]):
            continue
        speaker_matches += int(str(frame["speaker_id"]) == str(label["speaker_id"]))
        speech_matches += int(bool(frame["is_speech"]) == bool(label["is_speech"]))
        valence_errors.append(abs(float(frame["valence"]) - float(label["valence"])))
        arousal_errors.append(abs(float(frame["arousal"]) - float(label["arousal"])))
    denominator = max(1, limit)
    return {
        "frames_compared": limit,
        "speaker_label_agreement": round(speaker_matches / denominator, 6),
        "speech_flag_agreement": round(speech_matches / denominator, 6),
        "valence_mae": round(math.fsum(valence_errors) / max(1, len(valence_errors)), 6),
        "arousal_mae": round(math.fsum(arousal_errors) / max(1, len(arousal_errors)), 6),
    }


class OfflineEngine:
    def __init__(self, config: EngineConfig, cache: ArtifactCache | None = None) -> None:
        self.config = config
        self.cache = cache if cache is not None else ArtifactCache()
        self.snapshots = SnapshotStore()
        self._snapshot_version = 0

    def analyze_pcm(
        self,
        pcm: np.ndarray,
        sample_rate: int,
        speakers: Sequence[str],
        allowed_speakers: Sequence[str],
        identity: Mapping[str, Any] | None = None,
        truth: Sequence[Mapping[str, Any]] | None = None,
        verify_recounts: bool = True,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        array = np.asarray(pcm, dtype=PCM_DTYPE)
        expected_frames = complete_frame_count(
            int(array.shape[0]),
            self.config.frame_samples(sample_rate),
            self.config.hop_samples(sample_rate),
        )
        if len(speakers) != expected_frames:
            from .features import validate_speakers

            validate_speakers(speakers, allowed_speakers, expected_frames)

        if use_cache:
            features, feature_report = self.cache.features_for_pcm(array, sample_rate, self.config)
            view, cache_report = self.cache.build_view(
                features,
                speakers,
                self.config,
                feature_report["feature_cache_key"],
                identity=identity,
                allowed_speakers=allowed_speakers,
            )
        else:
            from .features import extract_features, feature_cache_key, pcm_content_hash

            features = extract_features(array, sample_rate, self.config)
            pcm_hash = pcm_content_hash(array, sample_rate)
            feature_key = feature_cache_key(pcm_hash, sample_rate, self.config)
            feature_report = {
                "feature_cache_key": feature_key,
                "pcm_content_key": pcm_hash,
                "feature_cache_hit": False,
                "feature_recomputed": True,
            }
            view = build_view(
                features,
                speakers,
                self.config,
                identity=identity,
                allowed_speakers=allowed_speakers,
            )
            cache_report = {
                "mode": "cache_bypassed",
                "recomputed_frame_intervals": [[0, len(features) - 1]] if features else [],
                "reused_frame_intervals": [],
                "recomputed_segment_ids": [str(segment["segment_id"]) for segment in view["segments"]],
                "coverage_all_frames": True,
                "overlap_free": True,
            }

        view["feature_cache"] = feature_report
        view["cache"] = cache_report
        ignored_tail = int(array.shape[0]) - (
            expected_frames * self.config.hop_samples(sample_rate)
            + self.config.frame_samples(sample_rate)
            - self.config.hop_samples(sample_rate)
            if expected_frames
            else 0
        )
        view["audio"] = {
            "sample_rate": sample_rate,
            "sample_count": int(array.shape[0]),
            "complete_frame_count": expected_frames,
            "ignored_tail_samples": max(0, ignored_tail),
            "pcm_content_key": feature_report["pcm_content_key"],
        }
        view["verification"] = {
            "structure": validate_view_structure(view, self.config),
            "window_recount": [],
            "segment_recount": [],
        }
        if verify_recounts:
            for index in range(len(view["frames"])):
                recount_window(index, view["frames"], self.config)
            for segment in view["segments"]:
                recount_segment(segment, view["frames"], self.config)
            view["verification"]["window_recount"] = [
                {"frame_index": index, "ok": True, "business_code": "OK"}
                for index in range(len(view["frames"]))
            ]
            view["verification"]["segment_recount"] = [
                {
                    "segment_id": segment["segment_id"],
                    "ok": True,
                    "business_code": "OK",
                }
                for segment in view["segments"]
            ]
        if truth is not None:
            view["truth_alignment"] = truth_alignment(view["frames"], truth)
        self._snapshot_version += 1
        replacements = commit_view(self.snapshots, view, self._snapshot_version)
        view["snapshot_version"] = self._snapshot_version
        view["snapshots"] = self.snapshots.metadata()
        view["snapshot_replacements"] = replacements
        return view

    def analyze_dialog(
        self,
        manifest_entry: Mapping[str, Any],
        wav_path: str | Path,
        truth_path: str | Path,
        verify_recounts: bool = True,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        validate_manifest_dialog(manifest_entry, self.config)
        pcm, sample_rate = load_wav(wav_path)
        if sample_rate != int(manifest_entry["sample_rate"]):
            raise EngineError(
                E_CONFIG_INVALID,
                "WAV sample rate differs from the manifest",
                field="manifest.sample_rate",
                wav_sample_rate=sample_rate,
                manifest_sample_rate=int(manifest_entry["sample_rate"]),
            )
        expected_samples = round(int(manifest_entry["duration_ms"]) * sample_rate / 1000)
        if int(pcm.shape[0]) != expected_samples:
            raise EngineError(
                E_CONFIG_INVALID,
                "WAV duration differs from the manifest",
                field="manifest.duration_ms",
                wav_samples=int(pcm.shape[0]),
                expected_samples=expected_samples,
            )
        truth = load_jsonl(truth_path)
        frames_expected = complete_frame_count(
            expected_samples,
            self.config.frame_samples(sample_rate),
            self.config.hop_samples(sample_rate),
        )
        if len(truth) != frames_expected:
            raise EngineError(
                E_CONFIG_INVALID,
                "truth frame count is inconsistent with the audio",
                field="truth.frame_count",
                truth_frames=len(truth),
                expected_frames=frames_expected,
            )
        speakers = [str(label["speaker_id"]) for label in truth]
        return self.analyze_pcm(
            pcm,
            sample_rate,
            speakers,
            [str(item) for item in manifest_entry["speakers"]],
            identity={
                "dialog_id": str(manifest_entry["dialog_id"]),
                "execution_path": "offline_batch",
            },
            truth=truth,
            verify_recounts=verify_recounts,
            use_cache=use_cache,
        )
