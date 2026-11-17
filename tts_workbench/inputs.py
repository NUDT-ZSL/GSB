from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .config import AcousticConfig, load_config
from .errors import ConfigInvalid
from .model import Failure, InputBundle, LoadedUtterance, PhoneSource
from .util import sha256_file, sha256_json


CONFIG_HINTS = {
    "sample_rate",
    "frame_length_ms",
    "frame_shift_ms",
    "f0_min_hz",
    "f0_max_hz",
    "concurrency",
}


def discover_config(input_dir: Path) -> Path:
    preferred = input_dir / "acoustic_config.json"
    if preferred.is_file():
        return preferred

    candidates: list[Path] = []
    for path in sorted(input_dir.rglob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and len(CONFIG_HINTS.intersection(payload)) >= 5:
            candidates.append(path)
    if len(candidates) != 1:
        raise ConfigInvalid(
            "expected exactly one acoustic configuration JSON",
            details={"candidates": [str(path) for path in candidates]},
        )
    return candidates[0]


def _is_command_jsonl(path: Path) -> bool:
    name = path.name.casefold()
    return "command" in name or "edit" in name


def _audio_format_error(
    path: Path,
    message: str,
    *,
    utterance_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> Failure:
    return Failure(
        code="E_AUDIO_FORMAT",
        scope="audio",
        message=message,
        utterance_id=utterance_id,
        path=str(path),
        details=details or {},
    )


def _read_and_validate_wav(
    path: Path, config: AcousticConfig
) -> tuple[np.ndarray | None, Failure | None]:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
        if len(header) < 12 or header[:4] not in (b"RIFF", b"RIFX") or header[8:12] != b"WAVE":
            return None, _audio_format_error(path, "invalid RIFF/WAVE container header")
        if header[:4] == b"RIFX":
            return None, _audio_format_error(path, "big-endian RIFX WAV is not supported")

        info = sf.info(str(path))
        if str(info.format).upper() != "WAV":
            return None, _audio_format_error(path, f"container is not WAV: {info.format}")
        if str(info.subtype).upper() != "PCM_16":
            return None, _audio_format_error(
                path,
                f"expected 16-bit PCM, got {info.subtype}",
                details={"subtype": str(info.subtype)},
            )
        if info.channels != 1:
            return None, _audio_format_error(
                path,
                f"expected mono audio, got {info.channels} channels",
                details={"channels": info.channels},
            )
        if info.samplerate != config.sample_rate:
            return None, _audio_format_error(
                path,
                f"sample rate mismatch: expected {config.sample_rate}, got {info.samplerate}",
                details={"expected": config.sample_rate, "actual": info.samplerate},
            )

        audio, sample_rate = sf.read(str(path), dtype="int16", always_2d=True)
        if sample_rate != config.sample_rate:
            return None, _audio_format_error(path, "sample rate changed while reading")
        if audio.shape[1] != 1:
            return None, _audio_format_error(path, "channel layout changed while reading")
        mono = np.ascontiguousarray(audio[:, 0], dtype=np.int16)
        if mono.size == 0:
            return None, _audio_format_error(path, "audio contains no samples")
        return mono, None
    except Exception as exc:  # soundfile exposes backend-specific exceptions
        return None, _audio_format_error(path, f"cannot decode WAV: {exc}")


def _normalise_phone_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
    phonemes = payload.get("phonemes")
    if not isinstance(phonemes, list) or not phonemes:
        raise ValueError("phonemes must be a non-empty array")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(phonemes):
        if isinstance(item, str):
            result.append({"phone": item})
        elif isinstance(item, dict) and isinstance(item.get("phone"), str):
            result.append(dict(item))
        else:
            raise ValueError(f"phonemes[{index}] must be a string or object with phone")
    return result


def _optional_numeric_list(
    payload: dict[str, Any],
    phone_items: list[dict[str, Any]],
    key: str,
    *,
    minimum: float,
    maximum: float | None = None,
) -> list[float | None]:
    global_values = payload.get(key)
    result: list[float | None] = []
    if global_values is not None:
        if not isinstance(global_values, list) or len(global_values) != len(phone_items):
            raise ValueError(f"{key} length must match phonemes")
        for value in global_values:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{key} contains a non-numeric value")
            value = float(value)
            if not math.isfinite(value) or value < minimum or (
                maximum is not None and value > maximum
            ):
                raise ValueError(f"{key} value out of range: {value}")
            result.append(value)
        return result

    for index, item in enumerate(phone_items):
        value = item.get(key)
        if value is None:
            result.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"phonemes[{index}].{key} must be numeric")
        value = float(value)
        if not math.isfinite(value) or value < minimum or (
            maximum is not None and value > maximum
        ):
            raise ValueError(f"phonemes[{index}].{key} out of range: {value}")
        result.append(value)
    return result


def _estimate_f0(
    segment: np.ndarray, config: AcousticConfig
) -> tuple[float, float, float, float]:
    if segment.size == 0:
        return 0.0, 0.0, 0.0, 0.0
    values = segment.astype(np.float64)
    values -= float(values.mean())
    rms = float(np.sqrt(np.mean(values * values)))
    if rms < 20.0:
        return 0.0, min(1.0, rms / 8192.0), min(1.0, rms / 32768.0), 0.0

    min_lag = max(1, int(math.floor(config.sample_rate / config.f0_max_hz)))
    max_lag = min(
        segment.size - 1,
        int(math.ceil(config.sample_rate / config.f0_min_hz)),
    )
    if max_lag <= min_lag:
        return 0.0, 0.0, min(1.0, rms / 32768.0), 0.0

    best_lag = min_lag
    best_score = -1.0
    for lag in range(min_lag, max_lag + 1):
        left = values[:-lag]
        right = values[lag:]
        denominator = math.sqrt(float(np.dot(left, left) * np.dot(right, right))) + 1e-12
        score = float(np.dot(left, right)) / denominator
        if score > best_score + 1e-15:
            best_score = score
            best_lag = lag
    # Deterministic tie break: the first (largest-frequency) lag wins because
    # only a strictly larger score replaces the incumbent.
    voicing_score = max(0.0, min(1.0, best_score))
    f0_hz = config.sample_rate / best_lag
    if not config.f0_min_hz <= f0_hz <= config.f0_max_hz:
        f0_hz = 0.0
    energy = min(1.0, rms / 32768.0 * 4.0)
    diff = np.diff(values) if values.size > 1 else np.zeros(1, dtype=np.float64)
    noise_floor = min(1.0, float(np.median(np.abs(diff))) / 32768.0 * 4.0)
    return f0_hz, energy, noise_floor, voicing_score


def _derive_phone_sources(
    payload: dict[str, Any],
    phone_items: list[dict[str, Any]],
    durations: list[int],
    audio: np.ndarray,
    config: AcousticConfig,
) -> list[PhoneSource]:
    f0_values = _optional_numeric_list(
        payload, phone_items, "f0_hz", minimum=0.0, maximum=config.sample_rate / 2
    )
    energy_values = _optional_numeric_list(
        payload, phone_items, "energy", minimum=0.0, maximum=1.0
    )
    noise_values = _optional_numeric_list(
        payload, phone_items, "noise_floor", minimum=0.0, maximum=1.0
    )
    voicing_values = _optional_numeric_list(
        payload, phone_items, "voicing_score", minimum=0.0, maximum=1.0
    )

    result: list[PhoneSource] = []
    cursor = 0
    for index, item in enumerate(phone_items):
        duration = durations[index]
        start = cursor * config.frame_shift_samples
        end = (cursor + duration) * config.frame_shift_samples
        segment = audio[start:end]
        est_f0, est_energy, est_noise, est_voicing = _estimate_f0(segment, config)
        result.append(
            PhoneSource(
                phone=item["phone"],
                duration_frames=duration,
                f0_hz=est_f0 if f0_values[index] is None else float(f0_values[index]),
                energy=est_energy if energy_values[index] is None else float(energy_values[index]),
                noise_floor=(
                    est_noise if noise_values[index] is None else float(noise_values[index])
                ),
                voicing_score=(
                    est_voicing if voicing_values[index] is None else float(voicing_values[index])
                ),
            )
        )
        cursor += duration
    return result


def _label_failure(path: Path, line: int | None, message: str, *, utterance_id: str | None = None) -> Failure:
    return Failure(
        code="E_LABEL_INVALID",
        scope="label",
        message=message,
        utterance_id=utterance_id,
        path=str(path),
        line=line,
    )


def _read_label_records(paths: list[Path]) -> tuple[list[tuple[Path, int, dict[str, Any]]], list[Failure]]:
    records: list[tuple[Path, int, dict[str, Any]]] = []
    failures: list[Failure] = []
    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line_number, raw_line in enumerate(handle, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        payload = json.loads(raw_line)
                    except json.JSONDecodeError as exc:
                        failures.append(
                            _label_failure(path, line_number, f"invalid JSON object: {exc}")
                        )
                        continue
                    if not isinstance(payload, dict):
                        failures.append(
                            _label_failure(path, line_number, "label record must be an object")
                        )
                        continue
                    records.append((path, line_number, payload))
        except OSError as exc:
            failures.append(_label_failure(path, None, f"cannot read label file: {exc}"))
    return records, failures


def load_inputs(input_dir: Path) -> InputBundle:
    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise ConfigInvalid(f"input directory does not exist: {input_dir}")

    config_path = discover_config(input_dir)
    config = load_config(config_path)
    failures: list[Failure] = []

    wav_paths = sorted(path for path in input_dir.rglob("*.wav") if path.is_file())
    audio_by_path: dict[Path, np.ndarray] = {}
    invalid_audio_paths: set[Path] = set()
    for path in wav_paths:
        resolved = path.resolve()
        audio, failure = _read_and_validate_wav(resolved, config)
        if failure is not None:
            failures.append(failure)
            invalid_audio_paths.add(resolved)
            continue
        assert audio is not None
        audio_by_path[resolved] = audio

    label_paths = sorted(
        path for path in input_dir.rglob("*.jsonl") if path.is_file() and not _is_command_jsonl(path)
    )
    command_paths = sorted(
        path for path in input_dir.rglob("*.jsonl") if path.is_file() and _is_command_jsonl(path)
    )
    if not label_paths:
        failures.append(_label_failure(input_dir, None, "no label JSONL file was found"))
    records, read_failures = _read_label_records(label_paths)
    failures.extend(read_failures)

    id_counts: dict[str, int] = {}
    for _, _, payload in records:
        utterance_id = payload.get("utterance_id")
        if isinstance(utterance_id, str) and utterance_id:
            id_counts[utterance_id] = id_counts.get(utterance_id, 0) + 1

    wav_by_stem: dict[str, list[Path]] = {}
    for path in wav_paths:
        wav_by_stem.setdefault(path.stem, []).append(path.resolve())

    used_audio_paths: set[Path] = set()
    utterances: list[LoadedUtterance] = []
    duplicate_reported: set[str] = set()
    for label_path, line_number, payload in records:
        utterance_id = payload.get("utterance_id")
        if not isinstance(utterance_id, str) or not utterance_id:
            failures.append(_label_failure(label_path, line_number, "utterance_id is required"))
            continue
        if id_counts.get(utterance_id, 0) > 1:
            if utterance_id not in duplicate_reported:
                failures.append(
                    _label_failure(
                        label_path,
                        line_number,
                        f"duplicate utterance_id {utterance_id!r}",
                        utterance_id=utterance_id,
                    )
                )
                duplicate_reported.add(utterance_id)
            continue

        try:
            phone_items = _normalise_phone_list(payload)
            durations_raw = payload.get("duration_frames")
            if not isinstance(durations_raw, list) or len(durations_raw) != len(phone_items):
                raise ValueError("duration_frames length must match phonemes")
            durations: list[int] = []
            for index, value in enumerate(durations_raw):
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(
                        f"duration_frames[{index}] must be a positive integer"
                    )
                durations.append(value)
            if not all(item["phone"] for item in phone_items):
                raise ValueError("phone names must be non-empty")
        except ValueError as exc:
            failures.append(_label_failure(label_path, line_number, str(exc), utterance_id=utterance_id))
            continue

        wav_value = payload.get("wav", payload.get("audio"))
        wav_path: Path | None = None
        if isinstance(wav_value, str):
            candidate = (input_dir / wav_value).resolve()
            wav_path = candidate if candidate in audio_by_path else None
            if wav_path is None and candidate in invalid_audio_paths:
                continue
        else:
            candidates = wav_by_stem.get(utterance_id, [])
            if len(candidates) == 1:
                wav_path = candidates[0]
            elif len(candidates) > 1:
                failures.append(
                    _label_failure(
                        label_path,
                        line_number,
                        f"multiple WAV files match utterance_id {utterance_id!r}",
                        utterance_id=utterance_id,
                    )
                )
                continue
        if wav_path is None:
            failures.append(
                _label_failure(
                    label_path,
                    line_number,
                    f"no matching WAV found for utterance {utterance_id!r}",
                    utterance_id=utterance_id,
                )
            )
            continue

        audio = audio_by_path[wav_path]
        total_frames = sum(durations)
        required_samples = (total_frames - 1) * config.frame_shift_samples + config.frame_length_samples
        if audio.size < required_samples:
            failures.append(
                _audio_format_error(
                    wav_path,
                    "reference WAV is shorter than the labelled frame span",
                    utterance_id=utterance_id,
                    details={"required_samples": required_samples, "actual_samples": int(audio.size)},
                )
            )
            continue

        try:
            phones = _derive_phone_sources(payload, phone_items, durations, audio, config)
        except ValueError as exc:
            failures.append(_label_failure(label_path, line_number, str(exc), utterance_id=utterance_id))
            continue

        label_payload = {
            "utterance_id": utterance_id,
            "phonemes": phone_items,
            "duration_frames": durations,
            "f0_hz": [phone.f0_hz for phone in phones],
            "energy": [phone.energy for phone in phones],
            "noise_floor": [phone.noise_floor for phone in phones],
            "voicing_score": [phone.voicing_score for phone in phones],
            "audio_sha256": sha256_file(wav_path),
        }
        utterances.append(
            LoadedUtterance(
                utterance_id=utterance_id,
                label_path=label_path,
                label_line=line_number,
                wav_path=wav_path,
                audio=audio,
                audio_hash=label_payload["audio_sha256"],
                label_hash=sha256_json(label_payload),
                phones=phones,
            )
        )
        used_audio_paths.add(wav_path)

    for wav_path in wav_paths:
        resolved = wav_path.resolve()
        if resolved in invalid_audio_paths or resolved in used_audio_paths:
            continue
        failures.append(
            Failure(
                code="E_LABEL_INVALID",
                scope="label",
                message="WAV reference has no matching label record",
                path=str(resolved),
            )
        )

    if not utterances and not failures:
        failures.append(
            _label_failure(input_dir, None, "no valid utterance could be loaded")
        )

    label_hash = sha256_json(
        [
            {
                "utterance_id": utt.utterance_id,
                "label_hash": utt.label_hash,
                "audio_hash": utt.audio_hash,
            }
            for utt in sorted(utterances, key=lambda item: item.utterance_id)
        ]
    )
    # Preserve discovery order for processing while keeping failures stable.
    failures.sort(
        key=lambda item: (
            item.scope,
            item.path or "",
            item.line if item.line is not None else -1,
            item.utterance_id or "",
            item.code,
        )
    )
    return InputBundle(
        input_dir=input_dir,
        config=config,
        config_path=config_path,
        utterances=utterances,
        failures=failures,
        label_hash=label_hash,
        commands_paths=command_paths,
    )
