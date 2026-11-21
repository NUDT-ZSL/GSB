"""Configuration loading, validation, and immutable updates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .errors import fail
from .util import (
    is_finite_number,
    read_json,
    sha256_value,
    write_json_atomic,
)


REQUIRED_SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class SmoothingConfig:
    method: str = "centered_moving_average"
    window_frames: int = 3


@dataclass(frozen=True)
class VadConfig:
    noise_quantile: float = 0.2
    low_multiplier: float = 1.8
    high_multiplier: float = 3.2
    min_silence_ms: float = 80.0
    threshold_version: str = "threshold-v1"


@dataclass(frozen=True)
class RefineConfig:
    window_ms: float = 20.0
    min_slope: float = 0.00005


@dataclass(frozen=True)
class SpeakerConfig:
    labels: tuple[str, ...] = ("S01", "S02")
    templates: dict[str, dict[str, float]] = field(default_factory=dict)
    annotation_bonus: float = 0.35
    confidence_margin: float = 0.08


@dataclass(frozen=True)
class ExportConfig:
    schema_version: str = "export-v1"


@dataclass(frozen=True)
class ResourceConfig:
    max_envelope_frames: int = 200_000
    max_snapshot_entries: int = 5_000
    audio_chunk_samples: int = 4_096


@dataclass(frozen=True)
class PathConfig:
    samples_manifest: Path
    annotations_manifest: Path
    snapshots_dir: Path
    replay_dir: Path
    runtime_annotations: Path
    runtime_config: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    config_version: int
    global_seed: int
    sample_rate: int
    frame_ms: float
    frame_hop_ms: float
    smoothing: SmoothingConfig
    vad: VadConfig
    refine: RefineConfig
    speaker: SpeakerConfig
    export: ExportConfig
    resources: ResourceConfig
    paths: PathConfig

    def with_frame(self, frame_ms: float, frame_hop_ms: float) -> "AppConfig":
        return replace(
            self,
            config_version=self.config_version + 1,
            frame_ms=float(frame_ms),
            frame_hop_ms=float(frame_hop_ms),
        )

    def with_noise_quantile(self, quantile: float) -> "AppConfig":
        next_version = self.config_version + 1
        return replace(
            self,
            config_version=next_version,
            vad=replace(
                self.vad,
                noise_quantile=float(quantile),
                threshold_version=f"threshold-v{next_version}",
            ),
        )

    def snapshot_identity(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "global_seed": self.global_seed,
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "frame_hop_ms": self.frame_hop_ms,
            "smoothing": asdict(self.smoothing),
            "vad": asdict(self.vad),
            "refine": asdict(self.refine),
            "speaker": {
                "labels": list(self.speaker.labels),
                "templates": self.speaker.templates,
                "annotation_bonus": self.speaker.annotation_bonus,
                "confidence_margin": self.speaker.confidence_margin,
            },
            "export": asdict(self.export),
            "resources": asdict(self.resources),
        }

    def identity_hash(self) -> str:
        return sha256_value(self.snapshot_identity())


def _number(section: dict[str, Any], key: str, code: str) -> float:
    value = section.get(key)
    if not is_finite_number(value):
        fail(code, f"{key} must be a finite number", field=key, value=value)
    return float(value)


def _positive_int(value: Any, field_name: str, code: str = "E_CONFIG_INVALID") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        fail(code, f"{field_name} must be a positive integer", field=field_name, value=value)
    return value


def _resolve(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


def load_config(path: str | Path, base_dir: str | Path | None = None) -> AppConfig:
    config_path = Path(path).resolve()
    try:
        raw = read_json(config_path)
    except FileNotFoundError:
        fail("E_CONFIG_INVALID", "configuration file does not exist", path=str(config_path))
    except ValueError as exc:
        fail("E_CONFIG_INVALID", "configuration file is not valid JSON", path=str(config_path), error=str(exc))

    root = Path(base_dir).resolve() if base_dir else config_path.parent.parent.resolve()
    return config_from_dict(raw, root)


def config_from_dict(raw: dict[str, Any], base_dir: str | Path) -> AppConfig:
    if not isinstance(raw, dict):
        fail("E_CONFIG_INVALID", "configuration root must be an object")
    root = Path(base_dir).resolve()

    sample_rate = raw.get("sample_rate")
    if sample_rate != REQUIRED_SAMPLE_RATE:
        fail(
            "E_SAMPLE_RATE",
            "only 16 kHz mono PCM is supported",
            expected=REQUIRED_SAMPLE_RATE,
            actual=sample_rate,
        )

    frame_ms = _number(raw, "frame_ms", "E_FRAME_MS")
    frame_hop_ms = _number(raw, "frame_hop_ms", "E_FRAME_MS")
    if frame_ms <= 0:
        fail("E_FRAME_MS", "frame_ms must be greater than zero", value=frame_ms)
    if frame_hop_ms <= 0:
        fail("E_FRAME_MS", "frame_hop_ms must be greater than zero", value=frame_hop_ms)
    frame_samples = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    hop_samples = max(1, int(round(sample_rate * frame_hop_ms / 1000.0)))
    if hop_samples > frame_samples:
        fail(
            "E_FRAME_SHIFT",
            "frame hop must not exceed frame length",
            frame_ms=frame_ms,
            frame_hop_ms=frame_hop_ms,
            frame_samples=frame_samples,
            hop_samples=hop_samples,
        )

    smoothing_raw = raw.get("smoothing", {})
    smoothing = SmoothingConfig(
        method=str(smoothing_raw.get("method", "centered_moving_average")),
        window_frames=_positive_int(
            smoothing_raw.get("window_frames", 3), "smoothing.window_frames"
        ),
    )
    if smoothing.method != "centered_moving_average":
        fail(
            "E_CONFIG_INVALID",
            "unsupported smoothing method",
            method=smoothing.method,
            supported=["centered_moving_average"],
        )

    vad_raw = raw.get("vad", {})
    quantile = _number(vad_raw, "noise_quantile", "E_CONFIG_INVALID")
    low_multiplier = _number(vad_raw, "low_multiplier", "E_CONFIG_INVALID")
    high_multiplier = _number(vad_raw, "high_multiplier", "E_CONFIG_INVALID")
    min_silence_ms = _number(vad_raw, "min_silence_ms", "E_CONFIG_INVALID")
    if not 0.0 <= quantile < 1.0:
        fail("E_CONFIG_INVALID", "noise_quantile must be in [0, 1)", value=quantile)
    if low_multiplier <= 0 or high_multiplier <= low_multiplier:
        fail(
            "E_CONFIG_INVALID",
            "VAD thresholds require 0 < low_multiplier < high_multiplier",
            low_multiplier=low_multiplier,
            high_multiplier=high_multiplier,
        )
    if min_silence_ms < 0:
        fail("E_CONFIG_INVALID", "min_silence_ms cannot be negative", value=min_silence_ms)
    vad = VadConfig(
        noise_quantile=quantile,
        low_multiplier=low_multiplier,
        high_multiplier=high_multiplier,
        min_silence_ms=min_silence_ms,
        threshold_version=str(vad_raw.get("threshold_version", "threshold-v1")),
    )

    refine_raw = raw.get("refine", {})
    refine = RefineConfig(
        window_ms=_number(refine_raw, "window_ms", "E_CONFIG_INVALID"),
        min_slope=_number(refine_raw, "min_slope", "E_CONFIG_INVALID"),
    )
    if refine.window_ms < 0 or refine.min_slope < 0:
        fail("E_CONFIG_INVALID", "refine parameters cannot be negative")

    speaker_raw = raw.get("speaker", {})
    labels = tuple(str(label) for label in speaker_raw.get("labels", []))
    if not labels or len(set(labels)) != len(labels) or any(not label for label in labels):
        fail("E_CONFIG_INVALID", "speaker labels must be a non-empty unique string set", labels=labels)
    templates: dict[str, dict[str, float]] = {}
    for label, template in speaker_raw.get("templates", {}).items():
        if not isinstance(template, dict):
            fail("E_CONFIG_INVALID", "speaker template must be an object", speaker_label=label)
        templates[str(label)] = {
            "energy": _number(template, "energy", "E_CONFIG_INVALID"),
            "zcr": _number(template, "zcr", "E_CONFIG_INVALID"),
        }
    missing_templates = sorted(set(labels) - set(templates))
    if missing_templates:
        fail(
            "E_CONFIG_INVALID",
            "every controlled speaker label needs an acoustic template",
            missing_labels=missing_templates,
        )
    speaker = SpeakerConfig(
        labels=labels,
        templates=templates,
        annotation_bonus=_number(speaker_raw, "annotation_bonus", "E_CONFIG_INVALID"),
        confidence_margin=_number(speaker_raw, "confidence_margin", "E_CONFIG_INVALID"),
    )
    if speaker.annotation_bonus < 0 or speaker.confidence_margin < 0:
        fail("E_CONFIG_INVALID", "speaker bonus and confidence margin cannot be negative")

    export = ExportConfig(
        schema_version=str(raw.get("export", {}).get("schema_version", "export-v1"))
    )
    resources_raw = raw.get("resources", {})
    resources = ResourceConfig(
        max_envelope_frames=_positive_int(
            resources_raw.get("max_envelope_frames", 200_000), "resources.max_envelope_frames"
        ),
        max_snapshot_entries=_positive_int(
            resources_raw.get("max_snapshot_entries", 5_000), "resources.max_snapshot_entries"
        ),
        audio_chunk_samples=_positive_int(
            resources_raw.get("audio_chunk_samples", 4_096), "resources.audio_chunk_samples"
        ),
    )

    paths_raw = raw.get("paths", {})
    required_paths = (
        "samples_manifest",
        "annotations_manifest",
        "snapshots_dir",
        "replay_dir",
        "runtime_annotations",
    )
    missing_paths = [name for name in required_paths if name not in paths_raw]
    if missing_paths:
        fail("E_CONFIG_INVALID", "missing path configuration", missing=missing_paths)
    runtime_annotations_path = _resolve(root, paths_raw["runtime_annotations"])
    runtime_config_path = (
        _resolve(root, paths_raw["runtime_config"])
        if paths_raw.get("runtime_config")
        else runtime_annotations_path.parent / "config.current.json"
    )
    paths = PathConfig(
        samples_manifest=_resolve(root, paths_raw["samples_manifest"]),
        annotations_manifest=_resolve(root, paths_raw["annotations_manifest"]),
        snapshots_dir=_resolve(root, paths_raw["snapshots_dir"]),
        replay_dir=_resolve(root, paths_raw["replay_dir"]),
        runtime_annotations=runtime_annotations_path,
        runtime_config=runtime_config_path,
    )

    config_version = raw.get("config_version", 1)
    _positive_int(config_version, "config_version")
    global_seed = raw.get("global_seed", 0)
    if isinstance(global_seed, bool) or not isinstance(global_seed, int):
        fail("E_CONFIG_INVALID", "global_seed must be an integer", value=global_seed)

    return AppConfig(
        config_version=config_version,
        global_seed=global_seed,
        sample_rate=sample_rate,
        frame_ms=frame_ms,
        frame_hop_ms=frame_hop_ms,
        smoothing=smoothing,
        vad=vad,
        refine=refine,
        speaker=speaker,
        export=export,
        resources=resources,
        paths=paths,
    )


def path_identity(config: AppConfig) -> dict[str, str]:
    return {
        "samples_manifest": str(config.paths.samples_manifest),
        "annotations_manifest": str(config.paths.annotations_manifest),
        "snapshots_dir": str(config.paths.snapshots_dir),
        "replay_dir": str(config.paths.replay_dir),
        "runtime_annotations": str(config.paths.runtime_annotations),
        "runtime_config": (
            str(config.paths.runtime_config)
            if config.paths.runtime_config is not None
            else ""
        ),
    }


def apply_runtime_identity(base: AppConfig, identity: dict[str, Any]) -> AppConfig:
    raw = dict(identity)
    raw["paths"] = path_identity(base)
    return config_from_dict(raw, base.paths.runtime_config.parent)


def write_runtime_config(config: AppConfig) -> None:
    if config.paths.runtime_config is None:
        return
    body = config.snapshot_identity()
    record = dict(body)
    record["identity_checksum"] = sha256_value(body)
    write_json_atomic(config.paths.runtime_config, record)


def load_runtime_config(path: str | Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    runtime_path = Path(path)
    if not runtime_path.exists():
        return None
    try:
        record = read_json(runtime_path)
    except (OSError, ValueError) as exc:
        fail(
            "E_CONFIG_INVALID",
            "runtime configuration is not valid JSON",
            path=str(runtime_path),
            error=str(exc),
        )
    checksum = record.get("identity_checksum") if isinstance(record, dict) else None
    body = (
        {key: value for key, value in record.items() if key != "identity_checksum"}
        if isinstance(record, dict)
        else None
    )
    if not isinstance(record, dict) or checksum != sha256_value(body):
        fail(
            "E_CONFIG_INVALID",
            "runtime configuration checksum mismatch",
            path=str(runtime_path),
        )
    return body
