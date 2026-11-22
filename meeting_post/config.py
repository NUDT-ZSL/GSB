"""Three-version meeting configuration reader, writer and migrator.

Canonical semantics are represented by MeetingConfig.  The three on-disk
forms deliberately look different:

* v1: one pipe-delimited positional line.
* v2: version header plus named pipe-delimited rows.
* v3: structured JSON.

The canonical config is the only object consumed by the processing pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from typing import Any

from .errors import MeetingError, corrupt, unsupported_version

SUPPORTED_VERSIONS = ("v1", "v2", "v3")
CANONICAL_DEFAULTS: dict[str, Any] = {
    "frame_ms": 20.0,
    "hop_ms": 20.0,
    "normalization": "rms",
    "variant": "balanced",
    "reid_threshold": 0.55,
    "mask_mode": "binary",
    "precision": "float64",
}


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def checksum(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


@dataclass(frozen=True)
class MeetingConfig:
    version: str
    sample_rate: int = 16000
    duration_ms: int = 4000
    max_speakers: int = 3
    threshold_multiplier: float = 1.8
    frame_ms: float = 20.0
    hop_ms: float = 20.0
    normalization: str = "rms"
    variant: str = "balanced"
    reid_threshold: float = 0.55
    speaker_gains: tuple[tuple[int, float], ...] = ((1, 1.0), (2, 1.0), (3, 1.0))
    mask_mode: str = "binary"
    precision: str = "float64"
    provenance: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def frame_samples(self) -> int:
        return int(round(self.frame_ms * self.sample_rate / 1000.0))

    @property
    def hop_samples(self) -> int:
        return int(round(self.hop_ms * self.sample_rate / 1000.0))

    @property
    def total_samples(self) -> int:
        return int(round(self.duration_ms * self.sample_rate / 1000.0))

    @property
    def frame_count(self) -> int:
        if self.hop_samples <= 0 or self.total_samples < self.frame_samples:
            return 0
        return 1 + (self.total_samples - self.frame_samples) // self.hop_samples

    @property
    def speaker_ids(self) -> tuple[int, ...]:
        return tuple(speaker_id for speaker_id, _ in self.speaker_gains)

    def gain_map(self) -> dict[int, float]:
        return dict(self.speaker_gains)

    def gain_for(self, speaker_id: int) -> float:
        return self.gain_map().get(speaker_id, 1.0)

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "duration_ms": self.duration_ms,
            "max_speakers": self.max_speakers,
            "threshold_multiplier": _clean_float(self.threshold_multiplier),
            "frame_ms": _clean_float(self.frame_ms),
            "hop_ms": _clean_float(self.hop_ms),
            "normalization": self.normalization,
            "variant": self.variant,
            "reid_threshold": _clean_float(self.reid_threshold),
            "speaker_gains": {
                str(speaker_id): _clean_float(gain)
                for speaker_id, gain in sorted(self.speaker_gains)
            },
            "mask_mode": self.mask_mode,
            "precision": self.precision,
        }

    def semantic_digest(self) -> str:
        return checksum(self.semantic_payload())

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            **self.semantic_payload(),
            "provenance": dict(sorted(self.provenance.items())),
        }

    def segmentation_payload(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "duration_ms": self.duration_ms,
            "max_speakers": self.max_speakers,
            "threshold_multiplier": _clean_float(self.threshold_multiplier),
            "frame_ms": _clean_float(self.frame_ms),
            "hop_ms": _clean_float(self.hop_ms),
            "normalization": self.normalization,
            "variant": self.variant,
        }

    def separation_payload(self) -> dict[str, Any]:
        return {
            "max_speakers": self.max_speakers,
            "speaker_gains": {
                str(speaker_id): _clean_float(gain)
                for speaker_id, gain in sorted(self.speaker_gains)
            },
            "mask_mode": self.mask_mode,
            "precision": self.precision,
        }

    def reid_payload(self) -> dict[str, Any]:
        return {
            "threshold": _clean_float(self.reid_threshold),
            "tie_break": ["speaker_id_ascending", "segment_start_sample_ascending"],
        }


def _clean_float(value: float) -> float:
    value = float(value)
    if value == 0:
        return 0.0
    if math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def default_config(version: str = "v3") -> MeetingConfig:
    gains = ((1, 1.0), (2, 1.0), (3, 1.0))
    config = MeetingConfig(version=version, speaker_gains=gains)
    _validate_config(config)
    return config


def validate_config(config: MeetingConfig) -> None:
    _validate_config(config)


def _validate_config(config: MeetingConfig) -> None:
    def invalid(field_name: str, reason: str) -> MeetingError:
        return MeetingError(
            "E_CONFIG_INVALID",
            f"invalid {field_name}: {reason}",
            {"field": field_name, "value": getattr(config, field_name, None)},
        )

    if config.version not in SUPPORTED_VERSIONS:
        raise unsupported_version(config.version, line=1)
    if config.sample_rate <= 0:
        raise invalid("sample_rate", "must be positive")
    if config.duration_ms <= 0:
        raise invalid("duration_ms", "must be positive")
    if config.max_speakers <= 0:
        raise invalid("max_speakers", "must be positive")
    if not math.isfinite(config.threshold_multiplier) or config.threshold_multiplier <= 0:
        raise invalid("threshold_multiplier", "must be a positive finite number")
    if not math.isfinite(config.frame_ms) or config.frame_ms <= 0:
        raise invalid("frame_ms", "must be a positive finite number")
    if not math.isfinite(config.hop_ms) or config.hop_ms <= 0:
        raise invalid("hop_ms", "must be a positive finite number")
    if config.hop_ms > config.frame_ms:
        raise invalid("hop_ms", "must not exceed frame_ms")
    if config.normalization not in {"rms", "peak", "none"}:
        raise invalid("normalization", "expected rms, peak or none")
    if not config.variant.strip():
        raise invalid("variant", "must not be empty")
    if not math.isfinite(config.reid_threshold) or not 0 <= config.reid_threshold <= 1:
        raise invalid("reid_threshold", "must be between 0 and 1")
    if config.mask_mode not in {"binary", "weighted"}:
        raise invalid("mask_mode", "expected binary or weighted")
    if config.precision not in {"float32", "float64"}:
        raise invalid("precision", "expected float32 or float64")
    ids = [speaker_id for speaker_id, _ in config.speaker_gains]
    if sorted(ids) != list(range(1, config.max_speakers + 1)):
        raise invalid(
            "speaker_gains",
            f"must contain exactly speaker ids 1..{config.max_speakers}",
        )
    if any(not math.isfinite(gain) or gain < 0 for _, gain in config.speaker_gains):
        raise invalid("speaker_gains", "gains must be non-negative finite numbers")
    if config.frame_samples <= 0 or config.hop_samples <= 0:
        raise invalid("frame_ms", "grid resolves to zero samples")
    if config.frame_ms != 20.0:
        raise invalid(
            "frame_ms",
            "truth-grid compatibility requires frame_ms=20 (hop_ms may be <= 20)",
        )


def _clean_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((line_number, stripped))
    return lines


def _parse_float(raw: str, *, line: int, field_name: str) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError) as exc:
        raise corrupt(
            f"field {field_name} is not numeric",
            line=line,
            field_name=field_name,
            raw=raw,
        ) from exc
    if not math.isfinite(value):
        raise corrupt(
            f"field {field_name} must be finite",
            line=line,
            field_name=field_name,
            raw=raw,
        )
    return value


def _parse_int(raw: str, *, line: int, field_name: str) -> int:
    value = _parse_float(raw, line=line, field_name=field_name)
    if not value.is_integer():
        raise corrupt(
            f"field {field_name} must be an integer",
            line=line,
            field_name=field_name,
            raw=raw,
        )
    return int(value)


def parse_config(text: str) -> MeetingConfig:
    lines = _clean_lines(text)
    if not lines:
        raise corrupt("configuration is empty", line=1, field_name="<document>")
    first_line_number, first = lines[0]
    if first.startswith("{"):
        return _parse_v3_json(text)

    first_parts = [part.strip() for part in first.split("|")]
    first_token = first_parts[0].lower()
    if first_token in {"v1", "v2", "v3"} and (len(first_parts) > 1 or first_token == "v2"):
        if first_token == "v1":
            return _parse_v1(lines)
        if first_token == "v2":
            return _parse_v2(lines)
        return _parse_v3_json(text)
    if first_token == "version" and len(first_parts) >= 2:
        version = first_parts[1].lower()
        if version == "v1":
            return _parse_v1(lines[1:])
        if version == "v2":
            return _parse_v2(lines)
        if version == "v3":
            return _parse_v3_json(text)
        raise unsupported_version(version, line=first_line_number)
    if first_token.startswith("v") and first_token[1:].isdigit():
        raise unsupported_version(first_token, line=first_line_number)

    named_fields = {}
    for line_number, line in lines:
        if "|" not in line:
            continue
        key, _, value = line.partition("|")
        named_fields[key.strip().lower()] = (line_number, value.strip())
    if "frame_ms" in named_fields or "normalization" in named_fields:
        return _parse_v2(lines)
    if len(first_parts) == 5:
        return _parse_v1(lines)
    raise corrupt(
        "could not dispatch configuration by version header or field set",
        line=first_line_number,
        field_name="<dispatch>",
    )


def _parse_v1(lines: list[tuple[int, str]]) -> MeetingConfig:
    if not lines:
        raise corrupt("v1 configuration is empty", line=1, field_name="<document>")
    line_number, first = lines[0]
    parts = [part.strip() for part in first.split("|")]
    expected = [
        "version",
        "sample_rate",
        "duration_ms",
        "max_speakers",
        "threshold_multiplier",
    ]
    if parts[0].lower() != "v1":
        raise corrupt("v1 header missing", line=line_number, field_name="version")
    if len(parts) != len(expected):
        missing = [name for name in expected[len(parts) :]]
        extra = len(parts) - len(expected)
        field_name = missing[0] if missing else f"<extra:{extra}>"
        raise corrupt(
            f"v1 expects {len(expected)} pipe-delimited columns, got {len(parts)}",
            line=line_number,
            field_name=field_name,
            expected_columns=expected,
            actual_columns=len(parts),
        )
    if len(lines) != 1:
        raise corrupt(
            "v1 accepts exactly one positional line",
            line=lines[1][0],
            field_name="<extra-row>",
        )
    sample_rate = _parse_int(parts[1], line=line_number, field_name="sample_rate")
    duration_ms = _parse_int(parts[2], line=line_number, field_name="duration_ms")
    max_speakers = _parse_int(parts[3], line=line_number, field_name="max_speakers")
    threshold_multiplier = _parse_float(
        parts[4], line=line_number, field_name="threshold_multiplier"
    )
    config = MeetingConfig(
        version="v1",
        sample_rate=sample_rate,
        duration_ms=duration_ms,
        max_speakers=max_speakers,
        threshold_multiplier=threshold_multiplier,
        frame_ms=float(CANONICAL_DEFAULTS["frame_ms"]),
        hop_ms=float(CANONICAL_DEFAULTS["hop_ms"]),
        normalization=str(CANONICAL_DEFAULTS["normalization"]),
        variant=str(CANONICAL_DEFAULTS["variant"]),
        reid_threshold=float(CANONICAL_DEFAULTS["reid_threshold"]),
        speaker_gains=tuple((speaker_id, 1.0) for speaker_id in range(1, max_speakers + 1)),
        mask_mode=str(CANONICAL_DEFAULTS["mask_mode"]),
        precision=str(CANONICAL_DEFAULTS["precision"]),
        provenance={
            "frame_ms": "v1_implied_default",
            "hop_ms": "v1_implied_default",
            "normalization": "v1_implied_default",
            "variant": "v1_implied_default",
            "reid_threshold": "v1_implied_default",
            "speaker_gains": "v1_implied_unit_gain",
            "mask_mode": "v1_implied_default",
            "precision": "v1_implied_default",
        },
    )
    _validate_config(config)
    return config


_V2_FIELDS = (
    "sample_rate",
    "duration_ms",
    "max_speakers",
    "threshold_multiplier",
    "frame_ms",
    "hop_ms",
    "normalization",
    "variant",
    "reid_threshold",
)


def _parse_v2(lines: list[tuple[int, str]]) -> MeetingConfig:
    if not lines:
        raise corrupt("v2 configuration is empty", line=1, field_name="<document>")
    first_line_number, first = lines[0]
    first_parts = [part.strip() for part in first.split("|")]
    version = "v2"
    body = lines
    if first_parts[0].lower() == "version":
        if len(first_parts) != 2:
            raise corrupt(
                "version row must have exactly two columns",
                line=first_line_number,
                field_name="version",
            )
        version = first_parts[1].lower()
        body = lines[1:]
    elif len(first_parts) == 1 and first_parts[0].lower() == "v2":
        body = lines[1:]
    if version != "v2":
        raise unsupported_version(version, line=first_line_number)
    rows: dict[str, tuple[int, str]] = {}
    for line_number, line in body:
        parts = [part.strip() for part in line.split("|")]
        if len(parts) != 2:
            raise corrupt(
                "v2 named row must contain exactly two pipe-delimited columns",
                line=line_number,
                field_name=parts[0] if parts else "<empty>",
                actual_columns=len(parts),
            )
        field_name, value = parts
        if field_name in rows:
            raise corrupt(
                f"duplicate v2 field {field_name}",
                line=line_number,
                field_name=field_name,
            )
        rows[field_name] = (line_number, value)
    for required in _V2_FIELDS:
        if required not in rows:
            raise corrupt(
                f"missing v2 field {required}",
                line=first_line_number,
                field_name=required,
            )
    unknown = sorted(set(rows) - set(_V2_FIELDS))
    if unknown:
        line_number = rows[unknown[0]][0]
        raise corrupt(
            f"unknown v2 field {unknown[0]}",
            line=line_number,
            field_name=unknown[0],
        )

    def numeric(field_name: str) -> float:
        line_number, raw = rows[field_name]
        return _parse_float(raw, line=line_number, field_name=field_name)

    def integer(field_name: str) -> int:
        line_number, raw = rows[field_name]
        return _parse_int(raw, line=line_number, field_name=field_name)

    sample_rate = integer("sample_rate")
    duration_ms = integer("duration_ms")
    max_speakers = integer("max_speakers")
    config = MeetingConfig(
        version="v2",
        sample_rate=sample_rate,
        duration_ms=duration_ms,
        max_speakers=max_speakers,
        threshold_multiplier=numeric("threshold_multiplier"),
        frame_ms=numeric("frame_ms"),
        hop_ms=numeric("hop_ms"),
        normalization=rows["normalization"][1],
        variant=rows["variant"][1],
        reid_threshold=numeric("reid_threshold"),
        speaker_gains=tuple((speaker_id, 1.0) for speaker_id in range(1, max_speakers + 1)),
        mask_mode=str(CANONICAL_DEFAULTS["mask_mode"]),
        precision=str(CANONICAL_DEFAULTS["precision"]),
        provenance={
            "speaker_gains": "v2_implied_unit_gain",
            "mask_mode": "v2_implied_default",
            "precision": "v2_implied_default",
        },
    )
    _validate_config(config)
    return config


def _json_line_for_key(text: str, key: str) -> int:
    needle = f'"{key}"'
    position = text.find(needle)
    if position < 0:
        return 1
    return text.count("\n", 0, position) + 1


def _v3_get(
    obj: dict[str, Any],
    path: tuple[str, ...],
    text: str,
    *,
    required: bool = True,
) -> Any:
    current: Any = obj
    for part in path:
        if not isinstance(current, dict) or part not in current:
            if not required:
                return None
            raise corrupt(
                f"missing v3 field {'.'.join(path)}",
                line=_json_line_for_key(text, path[-1]),
                field_name=".".join(path),
            )
        current = current[part]
    return current


def _v3_number(
    obj: dict[str, Any],
    path: tuple[str, ...],
    text: str,
    *,
    integer: bool = False,
) -> float | int:
    value = _v3_get(obj, path, text)
    field_name = ".".join(path)
    line = _json_line_for_key(text, path[-1])
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise corrupt(
            f"field {field_name} is not numeric",
            line=line,
            field_name=field_name,
            raw=value,
        )
    if not math.isfinite(float(value)):
        raise corrupt(
            f"field {field_name} must be finite",
            line=line,
            field_name=field_name,
            raw=value,
        )
    if integer:
        if not float(value).is_integer():
            raise corrupt(
                f"field {field_name} must be an integer",
                line=line,
                field_name=field_name,
                raw=value,
            )
        return int(value)
    return float(value)


def _parse_v3_json(text: str) -> MeetingConfig:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise corrupt(
            f"invalid JSON: {exc.msg}",
            line=exc.lineno,
            field_name="<json>",
            column=exc.colno,
        ) from exc
    if not isinstance(obj, dict):
        raise corrupt(
            "v3 document must be a JSON object",
            line=1,
            field_name="<document>",
        )
    version_value = obj.get("version", "v3")
    version = f"v{version_value}" if isinstance(version_value, int) else str(version_value).lower()
    if version != "v3":
        raise unsupported_version(version, line=_json_line_for_key(text, "version"))

    sample_rate = _v3_number(obj, ("session", "sample_rate"), text, integer=True)
    duration_ms = _v3_number(obj, ("session", "duration_ms"), text, integer=True)
    max_speakers = _v3_number(obj, ("session", "max_speakers"), text, integer=True)
    frame_ms = _v3_number(obj, ("segmentation", "frame_ms"), text)
    hop_ms = _v3_number(obj, ("segmentation", "hop_ms"), text)
    threshold_multiplier = _v3_number(
        obj, ("segmentation", "threshold_multiplier"), text
    )
    normalization = _v3_get(obj, ("segmentation", "normalization"), text)
    variant = _v3_get(obj, ("segmentation", "variant"), text)
    gains_raw = _v3_get(obj, ("separation", "speaker_gains"), text)
    mask_mode = _v3_get(obj, ("separation", "mask_mode"), text)
    precision = _v3_get(obj, ("separation", "precision"), text)
    reid_threshold = _v3_number(obj, ("reid", "threshold"), text)

    if not isinstance(gains_raw, dict):
        raise corrupt(
            "separation.speaker_gains must be an object keyed by speaker id",
            line=_json_line_for_key(text, "speaker_gains"),
            field_name="separation.speaker_gains",
        )
    gains: list[tuple[int, float]] = []
    for speaker_id in range(1, int(max_speakers) + 1):
        key = str(speaker_id)
        if key not in gains_raw:
            raise corrupt(
                f"missing speaker gain for speaker {speaker_id}",
                line=_json_line_for_key(text, "speaker_gains"),
                field_name=f"separation.speaker_gains.{key}",
            )
        raw_gain = gains_raw[key]
        if isinstance(raw_gain, bool) or not isinstance(raw_gain, (int, float)):
            raise corrupt(
                f"speaker gain {key} is not numeric",
                line=_json_line_for_key(text, "speaker_gains"),
                field_name=f"separation.speaker_gains.{key}",
                raw=raw_gain,
            )
        gains.append((speaker_id, float(raw_gain)))
    extra_keys = sorted(set(gains_raw) - {str(i) for i in range(1, int(max_speakers) + 1)})
    if extra_keys:
        raise corrupt(
            f"speaker gain key {extra_keys[0]} is outside 1..{max_speakers}",
            line=_json_line_for_key(text, "speaker_gains"),
            field_name=f"separation.speaker_gains.{extra_keys[0]}",
        )

    config = MeetingConfig(
        version="v3",
        sample_rate=int(sample_rate),
        duration_ms=int(duration_ms),
        max_speakers=int(max_speakers),
        threshold_multiplier=float(threshold_multiplier),
        frame_ms=float(frame_ms),
        hop_ms=float(hop_ms),
        normalization=str(normalization),
        variant=str(variant),
        reid_threshold=float(reid_threshold),
        speaker_gains=tuple(gains),
        mask_mode=str(mask_mode),
        precision=str(precision),
        provenance={},
    )
    _validate_config(config)
    return config


def render_config(config: MeetingConfig, version: str | None = None) -> str:
    target = version or config.version
    if target == "v1":
        return (
            f"v1|{config.sample_rate}|{config.duration_ms}|{config.max_speakers}|"
            f"{_format_number(config.threshold_multiplier)}\n"
        )
    if target == "v2":
        rows = [
            ("version", "v2"),
            ("sample_rate", config.sample_rate),
            ("duration_ms", config.duration_ms),
            ("max_speakers", config.max_speakers),
            ("threshold_multiplier", _format_number(config.threshold_multiplier)),
            ("frame_ms", _format_number(config.frame_ms)),
            ("hop_ms", _format_number(config.hop_ms)),
            ("normalization", config.normalization),
            ("variant", config.variant),
            ("reid_threshold", _format_number(config.reid_threshold)),
        ]
        return "\n".join(f"{key}|{value}" for key, value in rows) + "\n"
    if target == "v3":
        payload = {
            "format": "meeting-segment-config",
            "version": "v3",
            "session": {
                "sample_rate": config.sample_rate,
                "duration_ms": config.duration_ms,
                "max_speakers": config.max_speakers,
            },
            "segmentation": {
                "frame_ms": _clean_float(config.frame_ms),
                "hop_ms": _clean_float(config.hop_ms),
                "threshold_multiplier": _clean_float(config.threshold_multiplier),
                "normalization": config.normalization,
                "variant": config.variant,
            },
            "separation": {
                "speaker_gains": {
                    str(speaker_id): _clean_float(gain)
                    for speaker_id, gain in sorted(config.speaker_gains)
                },
                "mask_mode": config.mask_mode,
                "precision": config.precision,
            },
            "reid": {"threshold": _clean_float(config.reid_threshold)},
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    raise unsupported_version(target, line=1)


def _format_number(value: float) -> str:
    clean = _clean_float(value)
    return str(clean)


@dataclass
class MigrationAudit:
    source_version: str
    target_version: str
    lossy: bool
    additions: list[dict[str, Any]] = field(default_factory=list)
    drops: list[dict[str, Any]] = field(default_factory=list)
    implicit: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_version": self.source_version,
            "target_version": self.target_version,
            "lossy": self.lossy,
            "additions": self.additions,
            "drops": self.drops,
            "implicit": self.implicit,
        }


_TARGET_EXPLICIT_FIELDS = {
    "v1": {
        "sample_rate",
        "duration_ms",
        "max_speakers",
        "threshold_multiplier",
    },
    "v2": {
        "sample_rate",
        "duration_ms",
        "max_speakers",
        "threshold_multiplier",
        "frame_ms",
        "hop_ms",
        "normalization",
        "variant",
        "reid_threshold",
    },
    "v3": {
        "sample_rate",
        "duration_ms",
        "max_speakers",
        "threshold_multiplier",
        "frame_ms",
        "hop_ms",
        "normalization",
        "variant",
        "reid_threshold",
        "speaker_gains",
        "mask_mode",
        "precision",
    },
}


def _field_value(config: MeetingConfig, field_name: str) -> Any:
    if field_name == "speaker_gains":
        return {
            str(speaker_id): _clean_float(gain)
            for speaker_id, gain in sorted(config.speaker_gains)
        }
    value = getattr(config, field_name)
    if isinstance(value, float):
        return _clean_float(value)
    return value


def _implicit_value(field_name: str, max_speakers: int) -> Any:
    if field_name == "speaker_gains":
        return {str(speaker_id): 1 for speaker_id in range(1, max_speakers + 1)}
    return CANONICAL_DEFAULTS[field_name]


def migrate_config(
    config: MeetingConfig,
    target_version: str,
    *,
    lossy: bool = False,
) -> tuple[MeetingConfig, MigrationAudit]:
    if target_version not in SUPPORTED_VERSIONS:
        raise unsupported_version(target_version, line=1)
    audit = MigrationAudit(
        source_version=config.version,
        target_version=target_version,
        lossy=lossy,
    )
    target_fields = _TARGET_EXPLICIT_FIELDS[target_version]
    source_fields = _TARGET_EXPLICIT_FIELDS[config.version]
    all_fields = _TARGET_EXPLICIT_FIELDS["v3"]

    loss_fields: list[dict[str, Any]] = []
    for field_name in all_fields:
        value = _field_value(config, field_name)
        if field_name not in target_fields:
            implicit_value = _implicit_value(field_name, config.max_speakers)
            item = {
                "field": field_name,
                "source_value": value,
                "target_implicit": implicit_value,
            }
            if value != implicit_value:
                loss_fields.append(item)
            else:
                audit.implicit.append(
                    {
                        **item,
                        "rule": "target_implicit_default_matches_source",
                    }
                )
            if lossy:
                rule = "flatten_to_unit_gain" if field_name == "speaker_gains" else "discard_use_target_default"
                audit.drops.append({**item, "rule": rule})
        elif field_name not in source_fields:
            audit.additions.append(
                {
                    "field": field_name,
                    "value": value,
                    "source": f"{config.version}_implied_default",
                }
            )

    if loss_fields and not lossy:
        raise MeetingError(
            "E_SEMANTIC_LOSS",
            "target configuration cannot faithfully represent source semantics",
            {
                "source_version": config.version,
                "target_version": target_version,
                "fields": [item["field"] for item in loss_fields],
                "losses": loss_fields,
                "hint": "rerun with lossy=True/--lossy to apply the declared flatten/discard rules",
            },
        )

    replacement: dict[str, Any] = {
        "version": target_version,
        "provenance": dict(config.provenance),
    }
    for field_name in all_fields:
        if field_name in target_fields:
            replacement[field_name] = getattr(config, field_name)
        else:
            default = _implicit_value(field_name, config.max_speakers)
            if field_name == "speaker_gains":
                replacement[field_name] = tuple(
                    (speaker_id, float(default[str(speaker_id)]))
                    for speaker_id in range(1, config.max_speakers + 1)
                )
            else:
                replacement[field_name] = default
            replacement["provenance"][field_name] = f"{target_version}_implicit_default"
    for item in audit.additions:
        replacement["provenance"][item["field"]] = str(item["source"])

    target = replace(config, **replacement)
    _validate_config(target)
    return target, audit
