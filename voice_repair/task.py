from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .audio import WavInfo, inspect_wav
from .errors import BusinessIssue, RepairError, TaskValidationError


STAGE_NAMES = ("dc", "declick", "denoise", "normalize")
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class DCParams:
    block_frames: int
    mode: str
    offset: float


@dataclass(frozen=True)
class DeclickParams:
    block_frames: int
    threshold: float
    radius: int


@dataclass(frozen=True)
class DenoiseParams:
    block_frames: int
    frame_size: int
    hop_size: int
    noise_frames: int
    oversubtraction: float
    floor_ratio: float


@dataclass(frozen=True)
class NormalizeParams:
    block_frames: int
    target_peak: float


@dataclass(frozen=True)
class RecordingSpec:
    recording_id: str
    wav_path: Path
    wav_info: WavInfo
    dc: DCParams
    declick: DeclickParams
    denoise: DenoiseParams
    normalize: NormalizeParams

    @property
    def sample_count(self) -> int:
        return self.wav_info.frames

    @property
    def sample_rate(self) -> int:
        return self.wav_info.sample_rate

    def stage_block_frames(self, stage: int) -> int:
        return (
            self.dc.block_frames,
            self.declick.block_frames,
            self.denoise.block_frames,
            self.normalize.block_frames,
        )[stage]

    def block_ranges(self, stage: int) -> list[tuple[int, int]]:
        size = self.stage_block_frames(stage)
        return [
            (start, min(start + size, self.sample_count))
            for start in range(0, self.sample_count, size)
        ]


@dataclass(frozen=True)
class Task:
    schema_version: int
    task_id: str
    seed: int
    parallelism: int
    round_time_budget_ns: int
    memory_budget_bytes: int
    recordings: tuple[RecordingSpec, ...]
    source_path: Path
    raw_sha256: str
    raw: dict[str, Any] = field(repr=False)

    def input_digests(self) -> dict[str, str]:
        return {record.recording_id: record.wav_info.sha256 for record in self.recordings}


@dataclass
class ValidationResult:
    task: Task | None
    issues: list[BusinessIssue]
    raw_recording_ids: list[str]


def _int_field(
    obj: dict[str, Any],
    key: str,
    code: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if key not in obj:
        raise RepairError(code, f"missing parameter {key}")
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RepairError("E_PARAM_TYPE", f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise RepairError(code, f"{key} must be in [{minimum}, {maximum}]")
    return value


def _float_field(
    obj: dict[str, Any],
    key: str,
    code: str,
    *,
    minimum: float,
    maximum: float,
    inclusive_min: bool = True,
) -> float:
    if key not in obj:
        raise RepairError(code, f"missing parameter {key}")
    value = obj[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RepairError("E_PARAM_TYPE", f"{key} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise RepairError("E_PARAM_FINITE", f"{key} must be finite")
    low_ok = result >= minimum if inclusive_min else result > minimum
    if not low_ok or result > maximum:
        left = "[" if inclusive_min else "("
        raise RepairError(code, f"{key} must be in {left}{minimum}, {maximum}]")
    return result


def _parse_dc(stage: object) -> DCParams:
    if not isinstance(stage, dict):
        raise RepairError("E_PARAM_TYPE", "dc stage must be an object")
    block_frames = _int_field(stage, "block_frames", "E_BLOCK_FRAMES", minimum=1, maximum=1_000_000)
    mode = stage.get("mode", "mean")
    if mode not in {"mean", "fixed"}:
        raise RepairError("E_PARAM_RANGE", "dc.mode must be 'mean' or 'fixed'")
    offset = _float_field(
        stage,
        "offset",
        "E_DC_OFFSET",
        minimum=-1.0,
        maximum=1.0,
    ) if "offset" in stage else 0.0
    return DCParams(block_frames=block_frames, mode=str(mode), offset=offset)


def _parse_declick(stage: object) -> DeclickParams:
    if not isinstance(stage, dict):
        raise RepairError("E_PARAM_TYPE", "declick stage must be an object")
    return DeclickParams(
        block_frames=_int_field(
            stage, "block_frames", "E_BLOCK_FRAMES", minimum=1, maximum=1_000_000
        ),
        threshold=_float_field(
            stage,
            "threshold",
            "E_DECLICK_THRESHOLD",
            minimum=0.0,
            maximum=10.0,
            inclusive_min=False,
        ),
        radius=_int_field(stage, "radius", "E_DECLICK_RADIUS", minimum=1, maximum=64),
    )


def _parse_denoise(stage: object) -> DenoiseParams:
    if not isinstance(stage, dict):
        raise RepairError("E_PARAM_TYPE", "denoise stage must be an object")
    frame_size = _int_field(
        stage, "frame_size", "E_DENOISE_FRAME_SIZE", minimum=32, maximum=8192
    )
    if frame_size & (frame_size - 1):
        raise RepairError("E_DENOISE_FRAME_SIZE", "frame_size must be a power of two")
    hop_size = _int_field(stage, "hop_size", "E_DENOISE_HOP_SIZE", minimum=1, maximum=8192)
    if hop_size > frame_size:
        raise RepairError("E_DENOISE_HOP_SIZE", "hop_size must not exceed frame_size")
    return DenoiseParams(
        block_frames=_int_field(
            stage, "block_frames", "E_BLOCK_FRAMES", minimum=1, maximum=1_000_000
        ),
        frame_size=frame_size,
        hop_size=hop_size,
        noise_frames=_int_field(
            stage, "noise_frames", "E_DENOISE_NOISE_FRAMES", minimum=1, maximum=10_000
        ),
        oversubtraction=_float_field(
            stage,
            "oversubtraction",
            "E_DENOISE_OVERSUBTRACTION",
            minimum=0.0,
            maximum=10.0,
        ),
        floor_ratio=_float_field(
            stage,
            "floor_ratio",
            "E_DENOISE_FLOOR",
            minimum=0.0,
            maximum=1.0,
        ),
    )


def _parse_normalize(stage: object) -> NormalizeParams:
    if not isinstance(stage, dict):
        raise RepairError("E_PARAM_TYPE", "normalize stage must be an object")
    return NormalizeParams(
        block_frames=_int_field(
            stage, "block_frames", "E_BLOCK_FRAMES", minimum=1, maximum=1_000_000
        ),
        target_peak=_float_field(
            stage,
            "target_peak",
            "E_NORMALIZE_PEAK",
            minimum=0.0,
            maximum=1.0,
            inclusive_min=False,
        ),
    )


def _parse_recording(raw: object, base_dir: Path) -> RecordingSpec:
    if not isinstance(raw, dict):
        raise RepairError("E_RECORDING_TYPE", "recording entry must be an object")
    recording_id = raw.get("id")
    if not isinstance(recording_id, str) or not ID_PATTERN.fullmatch(recording_id):
        raise RepairError("E_ID_INVALID", "id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    wav_name = raw.get("wav")
    if not isinstance(wav_name, str) or not wav_name:
        raise RepairError("E_WAV_PATH", "wav must be a non-empty path")
    wav_path = Path(wav_name)
    if not wav_path.is_absolute():
        wav_path = base_dir / wav_path
    if not wav_path.is_file():
        raise RepairError("E_FILE_MISSING", f"WAV file not found: {wav_path}")
    info = inspect_wav(wav_path)

    stages = raw.get("stages")
    if not isinstance(stages, dict):
        raise RepairError("E_STAGES_MISSING", "stages must be an object")
    unknown = sorted(set(stages) - set(STAGE_NAMES))
    if unknown:
        raise RepairError("E_STAGE_UNKNOWN", f"unknown stages: {', '.join(unknown)}")
    missing = [name for name in STAGE_NAMES if name not in stages]
    if missing:
        raise RepairError("E_STAGE_MISSING", f"missing stages: {', '.join(missing)}")

    return RecordingSpec(
        recording_id=recording_id,
        wav_path=wav_path.resolve(),
        wav_info=info,
        dc=_parse_dc(stages["dc"]),
        declick=_parse_declick(stages["declick"]),
        denoise=_parse_denoise(stages["denoise"]),
        normalize=_parse_normalize(stages["normalize"]),
    )


def load_task(path: Path) -> ValidationResult:
    path = path.resolve()
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise TaskValidationError("E_TASK_READ", f"cannot read task JSON: {exc}") from exc
    try:
        raw = json.loads(raw_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskValidationError("E_TASK_JSON", f"invalid task JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise TaskValidationError("E_TASK_JSON", "task root must be an object")

    if raw.get("schema_version") != 1:
        raise TaskValidationError("E_TASK_VERSION", "schema_version must be 1")
    task_id = raw.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise TaskValidationError("E_TASK_ID", "task_id must be a non-empty string")
    seed = raw.get("seed", 0)
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TaskValidationError("E_TASK_SEED", "seed must be an integer")
    parallelism = raw.get("parallelism")
    if isinstance(parallelism, bool) or not isinstance(parallelism, int) or not 1 <= parallelism <= 64:
        raise TaskValidationError("E_PARALLELISM", "parallelism must be an integer in [1, 64]")
    round_budget = raw.get("round_time_budget_ns")
    if (
        isinstance(round_budget, bool)
        or not isinstance(round_budget, int)
        or round_budget <= 0
    ):
        raise TaskValidationError(
            "E_ROUND_BUDGET", "round_time_budget_ns must be a positive integer"
        )
    memory_budget = raw.get("memory_budget_bytes")
    if (
        isinstance(memory_budget, bool)
        or not isinstance(memory_budget, int)
        or memory_budget <= 0
    ):
        raise TaskValidationError(
            "E_MEMORY_BUDGET", "memory_budget_bytes must be a positive integer"
        )
    raw_recordings = raw.get("recordings")
    if not isinstance(raw_recordings, list) or not raw_recordings:
        raise TaskValidationError("E_RECORDINGS", "recordings must be a non-empty array")

    issues: list[BusinessIssue] = []
    raw_ids: list[str] = []
    seen: set[str] = set()
    duplicate_ids: set[str] = set()
    for index, item in enumerate(raw_recordings):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            candidate = item["id"]
        else:
            candidate = f"<entry:{index}>"
        raw_ids.append(candidate)
        if candidate in seen:
            duplicate_ids.add(candidate)
        seen.add(candidate)

    for duplicate_id in sorted(duplicate_ids):
        issues.append(
            BusinessIssue(duplicate_id, "E_ID_DUPLICATE", "recording id is duplicated")
        )

    recordings: list[RecordingSpec] = []
    for i, item in enumerate(raw_recordings):
        candidate = raw_ids[i]
        if candidate in duplicate_ids:
            continue
        try:
            recordings.append(_parse_recording(item, path.parent))
        except RepairError as exc:
            issues.append(BusinessIssue(candidate, exc.code, exc.message))

    task = Task(
        schema_version=1,
        task_id=task_id,
        seed=seed,
        parallelism=parallelism,
        round_time_budget_ns=round_budget,
        memory_budget_bytes=memory_budget,
        recordings=tuple(recordings),
        source_path=path,
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        raw=raw,
    )
    return ValidationResult(task=task, issues=issues, raw_recording_ids=raw_ids)
