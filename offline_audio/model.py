"""Manifest parsing, normalization, and structural validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .errors import BusinessError


_MISSING = object()


def _get_alias(mapping: dict[str, Any], names: Sequence[str], default: Any = _MISSING) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    if default is _MISSING:
        raise KeyError(names[0])
    return default


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(
            "INVALID_MANIFEST", f"{label} 必须是 JSON 对象", {"field": label}
        )
    return value


def _as_int(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    code: str = "INVALID_MANIFEST",
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(
            code, f"{label} 必须是整数", {"field": label, "value": value}
        )
    if minimum is not None and value < minimum:
        raise BusinessError(
            code,
            f"{label} 必须大于等于 {minimum}",
            {"field": label, "value": value, "minimum": minimum},
        )
    return value


def _as_finite_float(
    value: Any, label: str, *, code: str = "INVALID_MANIFEST"
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(
            code, f"{label} 必须是有限浮点数", {"field": label, "value": value}
        )
    result = float(value)
    if not math.isfinite(result):
        raise BusinessError(
            code, f"{label} 必须是有限浮点数", {"field": label, "value": value}
        )
    return result


def _as_positive_number(
    value: Any, label: str, *, code: str = "INVALID_MANIFEST"
) -> float:
    result = _as_finite_float(value, label, code=code)
    if result <= 0:
        raise BusinessError(
            code, f"{label} 必须大于 0", {"field": label, "value": value}
        )
    return result


def _resolve_path(value: Any, base: Path, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise BusinessError(
            "INVALID_MANIFEST", f"{label} 必须是非空路径字符串", {"field": label}
        )
    path = Path(os.path.expandvars(os.path.expanduser(value)))
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def round_half_up(value: float) -> int:
    return int(math.floor(value + 0.5))


def milliseconds_to_samples(milliseconds: float | int, sample_rate: int) -> int:
    return round_half_up(float(milliseconds) * float(sample_rate) / 1000.0)


@dataclass(frozen=True)
class Silence:
    start_ms: float
    end_ms: float


@dataclass
class TrackSpec:
    index: int
    path: Path
    sample_rate: int
    bit_depth: int
    offset_ms: int
    gain: float
    silences: list[Silence] = field(default_factory=list)
    channel_map: list[int] | None = None
    content_sha256: str = ""
    actual_sample_rate: int = 0
    actual_channels: int = 0
    actual_bit_depth: int = 0
    codec_name: str = ""
    duration_seconds: float = 0.0
    offset_samples: int = 0

    @property
    def duration_ms(self) -> float:
        return self.duration_seconds * 1000.0


@dataclass(frozen=True)
class OutputSpec:
    path: Path
    report_path: Path
    state_path: Path
    work_dir: Path
    sample_rate: int
    channels: int
    channel_layout: tuple[str, ...]
    bit_depth: int
    sample_format: str
    window_ms: float
    window_frames: int
    peak_policy: str
    max_workers: int
    memory_limit_mb: int
    duration_ms: float | None


@dataclass
class Job:
    manifest_path: Path
    tracks: list[TrackSpec]
    output: OutputSpec
    snapshot_payload: dict[str, Any]
    snapshot_id: str = ""

    def assign_snapshot(self) -> None:
        canonical = json.dumps(
            self.snapshot_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.snapshot_id = hashlib.sha256(canonical).hexdigest()


_LAYOUT_ALIASES = {
    "mono": ("mono",),
    "m": ("mono",),
    "1": ("mono",),
    "stereo": ("left", "right"),
    "2": ("left", "right"),
    "l": ("left",),
    "left": ("left",),
    "fl": ("left",),
    "front_left": ("left",),
    "r": ("right",),
    "right": ("right",),
    "fr": ("right",),
    "front_right": ("right",),
    "c": ("center",),
    "center": ("center",),
    "fc": ("center",),
    "lfe": ("lfe",),
    "low_frequency": ("lfe",),
    "bl": ("surround_left",),
    "back_left": ("surround_left",),
    "sl": ("surround_left",),
    "side_left": ("surround_left",),
    "br": ("surround_right",),
    "back_right": ("surround_right",),
    "sr": ("surround_right",),
    "side_right": ("surround_right",),
}

_NAMED_LAYOUTS = {
    "quad": ("left", "right", "surround_left", "surround_right"),
    "4.0": ("left", "right", "surround_left", "surround_right"),
    "5.0": ("left", "right", "center", "surround_left", "surround_right"),
    "5.1": ("left", "right", "center", "lfe", "surround_left", "surround_right"),
    "5.1(side)": ("left", "right", "center", "lfe", "surround_left", "surround_right"),
    "7.1": (
        "left",
        "right",
        "center",
        "lfe",
        "surround_left",
        "surround_right",
        "side_left",
        "side_right",
    ),
}


def _normalise_layout(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _NAMED_LAYOUTS:
            return _NAMED_LAYOUTS[key]
        if key in _LAYOUT_ALIASES:
            return _LAYOUT_ALIASES[key]
        parts = [part.strip().lower() for part in key.replace("+", ",").split(",") if part.strip()]
        if not parts:
            raise BusinessError(
                "INVALID_OUTPUT_SPEC",
                "channel_layout 不能为空",
                {"field": "output.channel_layout"},
            )
    elif isinstance(value, list):
        if not value or not all(isinstance(part, str) for part in value):
            raise BusinessError(
                "INVALID_OUTPUT_SPEC",
                "channel_layout 必须是非空字符串数组",
                {"field": "output.channel_layout"},
            )
        parts = [part.strip().lower() for part in value]
    else:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "channel_layout 必须是字符串或字符串数组",
            {"field": "output.channel_layout"},
        )

    result: list[str] = []
    for part in parts:
        if part in _NAMED_LAYOUTS:
            result.extend(_NAMED_LAYOUTS[part])
        elif part in _LAYOUT_ALIASES:
            result.extend(_LAYOUT_ALIASES[part])
        else:
            result.append(part)
    return tuple(result)


def _parse_silences(raw: Any, index: int) -> list[Silence]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BusinessError(
            "INVALID_MANIFEST",
            "tracks[].silences 必须是数组",
            {"field": f"tracks[{index}].silences"},
        )
    result: list[Silence] = []
    for silence_index, item in enumerate(raw):
        label = f"tracks[{index}].silences[{silence_index}]"
        if isinstance(item, (list, tuple)):
            if len(item) != 2:
                raise BusinessError(
                    "INVALID_MANIFEST", f"{label} 必须包含起止毫秒", {"field": label}
                )
            start_raw, end_raw = item
        elif isinstance(item, dict):
            try:
                start_raw = _get_alias(item, ("start_ms", "start", "from_ms", "from"))
                end_raw = _get_alias(item, ("end_ms", "end", "to_ms", "to"))
            except KeyError as exc:
                raise BusinessError(
                    "INVALID_MANIFEST",
                    f"{label} 缺少 start_ms/end_ms",
                    {"field": label},
                ) from exc
        else:
            raise BusinessError(
                "INVALID_MANIFEST", f"{label} 必须是对象或二元数组", {"field": label}
            )
        start_ms = _as_finite_float(start_raw, f"{label}.start_ms")
        end_ms = _as_finite_float(end_raw, f"{label}.end_ms")
        if start_ms < 0 or end_ms < 0:
            raise BusinessError(
                "INVALID_MANIFEST",
                f"{label} 的起止毫秒不得为负数",
                {"field": label, "start_ms": start_ms, "end_ms": end_ms},
            )
        if end_ms < start_ms:
            raise BusinessError(
                "INVALID_MANIFEST",
                f"{label}.end_ms 不得早于 start_ms",
                {"field": label, "start_ms": start_ms, "end_ms": end_ms},
            )
        result.append(Silence(start_ms=start_ms, end_ms=end_ms))
    return result


def _parse_track(
    raw: Any,
    index: int,
    manifest_dir: Path,
) -> TrackSpec:
    item = _require_object(raw, f"tracks[{index}]")
    try:
        file_raw = _get_alias(item, ("file", "path", "file_path"))
        sample_rate_raw = _get_alias(item, ("sample_rate", "sampleRate", "sample_rate_hz"))
        bit_depth_raw = _get_alias(item, ("bit_depth", "bitDepth", "bits_per_sample"))
        offset_raw = _get_alias(
            item, ("offset_ms", "offsetMs", "start_ms", "startOffsetMs", "start_offset_ms")
        )
    except KeyError as exc:
        raise BusinessError(
            "INVALID_MANIFEST",
            f"tracks[{index}] 缺必填字段 {exc.args[0]}",
            {"field": f"tracks[{index}].{exc.args[0]}"},
        ) from exc

    path = _resolve_path(file_raw, manifest_dir, f"tracks[{index}].file")
    sample_rate = _as_int(sample_rate_raw, f"tracks[{index}].sample_rate", minimum=1)
    if sample_rate > 768000:
        raise BusinessError(
            "INVALID_MANIFEST",
            "tracks[].sample_rate 超出支持范围",
            {"field": f"tracks[{index}].sample_rate", "value": sample_rate},
        )
    bit_depth = _as_int(bit_depth_raw, f"tracks[{index}].bit_depth", minimum=1)
    if bit_depth not in (8, 16, 24, 32, 64):
        raise BusinessError(
            "INVALID_MANIFEST",
            "tracks[].bit_depth 必须是 8/16/24/32/64",
            {"field": f"tracks[{index}].bit_depth", "value": bit_depth},
        )
    offset_ms = _as_int(offset_raw, f"tracks[{index}].offset_ms", minimum=0)
    gain = _as_finite_float(item.get("gain", 1.0), f"tracks[{index}].gain")
    silences = _parse_silences(item.get("silences", item.get("mute", [])), index)

    channel_map_raw = item.get("channel_map", item.get("channelMap"))
    channel_map: list[int] | None = None
    if channel_map_raw is not None:
        if not isinstance(channel_map_raw, list):
            raise BusinessError(
                "INVALID_MANIFEST",
                "tracks[].channel_map 必须是整数数组",
                {"field": f"tracks[{index}].channel_map"},
            )
        channel_map = []
        for map_index, value in enumerate(channel_map_raw):
            if isinstance(value, bool) or not isinstance(value, int) or value < -1:
                raise BusinessError(
                    "INVALID_MANIFEST",
                    "channel_map 元素必须是大于等于 -1 的整数",
                    {
                        "field": f"tracks[{index}].channel_map[{map_index}]",
                        "value": value,
                    },
                )
            channel_map.append(value)

    return TrackSpec(
        index=index,
        path=path,
        sample_rate=sample_rate,
        bit_depth=bit_depth,
        offset_ms=offset_ms,
        gain=gain,
        silences=silences,
        channel_map=channel_map,
    )


_PCM_ENCODERS = {
    8: "pcm_u8",
    16: "pcm_s16le",
    24: "pcm_s24le",
    32: "pcm_s32le",
}


def _parse_output(
    raw: dict[str, Any],
    manifest_dir: Path,
    *,
    output_override: str | None,
    report_override: str | None,
    state_override: str | None,
    work_override: str | None,
    cli_base: Path,
    concurrency_override: int | None,
    memory_override: int | None,
    window_override: float | None,
) -> OutputSpec:
    if "output" in raw:
        data = _require_object(raw["output"], "output")
    elif "output_spec" in raw:
        data = _require_object(raw["output_spec"], "output_spec")
    else:
        flat_keys = {
            "sample_rate",
            "sampleRate",
            "sample_rate_hz",
            "file",
            "output_file",
            "output_path",
            "channels",
            "channel_count",
            "channel_layout",
            "bit_depth",
            "bitDepth",
            "sample_format",
            "statistics_window_ms",
            "stats_window_ms",
            "window_ms",
            "duration_ms",
            "max_workers",
            "concurrency",
            "memory_limit_mb",
            "peak_policy",
        }
        data = raw if any(key in raw for key in flat_keys) else {}

    try:
        sample_rate_raw = _get_alias(
            data, ("sample_rate", "sampleRate", "sample_rate_hz")
        )
    except KeyError:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出规格缺少 sample_rate",
            {"field": "output.sample_rate"},
        ) from None
    sample_rate = _as_int(
        sample_rate_raw, "output.sample_rate", minimum=1, code="INVALID_OUTPUT_SPEC"
    )
    if sample_rate > 768000:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "output.sample_rate 超出支持范围",
            {"field": "output.sample_rate", "value": sample_rate},
        )

    try:
        output_raw = output_override or _get_alias(
            data, ("file", "path", "output_file", "output_path")
        )
    except KeyError:
        output_raw = None
    if output_raw is None:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "缺少输出 WAV 路径（--output 或 output.file）",
            {"field": "output.file"},
        )
    output_base = cli_base if output_override else manifest_dir
    output_path = _resolve_path(output_raw, output_base, "output.file")
    if output_path.suffix.lower() != ".wav":
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出文件必须使用 .wav 扩展名",
            {"field": "output.file", "value": str(output_path)},
        )

    layout_raw = data.get("channel_layout", data.get("channelLayout"))
    channels_raw = data.get("channels", data.get("channel_count", data.get("channelCount")))
    if layout_raw is not None:
        layout = _normalise_layout(layout_raw)
    elif channels_raw is not None:
        channels_value = _as_int(
            channels_raw, "output.channels", minimum=1, code="INVALID_OUTPUT_SPEC"
        )
        if channels_value == 1:
            layout = ("mono",)
        elif channels_value == 2:
            layout = ("left", "right")
        else:
            layout = tuple(f"channel_{i}" for i in range(channels_value))
    else:
        layout = ("left", "right")

    if channels_raw is not None:
        channels = _as_int(
            channels_raw, "output.channels", minimum=1, code="INVALID_OUTPUT_SPEC"
        )
        if channels != len(layout):
            raise BusinessError(
                "INVALID_OUTPUT_SPEC",
                "output.channels 与 channel_layout 数量不一致",
                {
                    "field": "output.channels",
                    "channels": channels,
                    "layout_channels": len(layout),
                },
            )
    else:
        channels = len(layout)
    if channels > 16:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出声道数不得超过 16",
            {"field": "output.channels", "value": channels},
        )

    bit_depth = _as_int(
        data.get("bit_depth", data.get("bitDepth", 16)),
        "output.bit_depth",
        code="INVALID_OUTPUT_SPEC",
    )
    sample_format = str(data.get("sample_format", data.get("sampleFormat", ""))).lower()
    if sample_format in ("", "int", "integer", "pcm"):
        if bit_depth not in _PCM_ENCODERS:
            raise BusinessError(
                "INVALID_OUTPUT_SPEC",
                "整数输出位深必须是 8/16/24/32",
                {"field": "output.bit_depth", "value": bit_depth},
            )
        sample_format = _PCM_ENCODERS[bit_depth]
    elif sample_format in ("float", "float32", "f32", "pcm_f32le"):
        if bit_depth != 32:
            raise BusinessError(
                "INVALID_OUTPUT_SPEC",
                "浮点输出必须使用 bit_depth=32",
                {"field": "output.bit_depth", "value": bit_depth},
            )
        sample_format = "pcm_f32le"
    else:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "不支持的 output.sample_format",
            {"field": "output.sample_format", "value": sample_format},
        )

    window_source = (
        window_override
        if window_override is not None
        else _get_alias(
            data,
            ("statistics_window_ms", "stats_window_ms", "window_ms"),
            default=1000.0,
        )
    )
    window_ms = _as_positive_number(
        window_source, "output.statistics_window_ms", code="INVALID_OUTPUT_SPEC"
    )
    window_frames = milliseconds_to_samples(window_ms, sample_rate)
    if window_frames <= 0:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "统计窗口换算后必须至少包含 1 个采样点",
            {"field": "output.statistics_window_ms", "value": window_ms},
        )

    workers_source = (
        concurrency_override
        if concurrency_override is not None
        else _get_alias(data, ("max_workers", "concurrency", "workers"), default=4)
    )
    max_workers = _as_int(
        workers_source, "output.max_workers", minimum=1, code="INVALID_OUTPUT_SPEC"
    )
    if max_workers > 64:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "output.max_workers 不得超过 64",
            {"field": "output.max_workers", "value": max_workers},
        )

    memory_source = (
        memory_override
        if memory_override is not None
        else _get_alias(
            data,
            ("memory_limit_mb", "memory_budget_mb", "memoryLimitMb"),
            default=512,
        )
    )
    memory_limit_mb = _as_int(
        memory_source, "output.memory_limit_mb", minimum=1, code="INVALID_OUTPUT_SPEC"
    )

    peak_policy = str(
        _get_alias(data, ("peak_policy", "limit_policy", "overflow"), default="clip")
    ).strip().lower()
    peak_policy = {"normalize": "scale", "limiter": "clip"}.get(peak_policy, peak_policy)
    if peak_policy not in ("clip", "scale"):
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "output.peak_policy 必须是 clip 或 scale",
            {"field": "output.peak_policy", "value": peak_policy},
        )

    duration_raw = data.get("duration_ms", data.get("durationMs"))
    duration_ms: float | None = None
    if duration_raw is not None:
        duration_ms = _as_positive_number(
            duration_raw, "output.duration_ms", code="INVALID_OUTPUT_SPEC"
        )

    report_raw = report_override or raw.get(
        "report_file",
        raw.get(
            "report_path",
            data.get("report_file", data.get("report_path")),
        ),
    )
    if report_raw is None:
        report_path = output_path.with_name(output_path.name + ".report.json")
    else:
        report_path = _resolve_path(
            report_raw,
            cli_base if report_override else manifest_dir,
            "report_file",
        )

    state_raw = state_override or raw.get(
        "state_file",
        raw.get(
            "state_path",
            data.get("state_file", data.get("state_path")),
        ),
    )
    if state_raw is None:
        state_path = output_path.with_name(output_path.name + ".state.json")
    else:
        state_path = _resolve_path(
            state_raw,
            cli_base if state_override else manifest_dir,
            "state_file",
        )

    work_raw = work_override or raw.get(
        "work_dir",
        data.get("work_dir", data.get("workDir")),
    )
    if work_raw is None:
        work_dir = output_path.parent / f".{output_path.stem}.work"
    else:
        work_dir = _resolve_path(
            work_raw,
            cli_base if work_override else manifest_dir,
            "work_dir",
        )
    if work_dir.exists() and not work_dir.is_dir():
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "work_dir 不是目录",
            {"field": "work_dir", "value": str(work_dir)},
        )

    special_paths = [output_path, report_path, state_path]
    if len(set(special_paths)) != len(special_paths) or work_dir in special_paths:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出、报告、状态与工作目录路径不得冲突",
            {
                "paths": [str(path) for path in special_paths],
                "work_dir": str(work_dir),
            },
        )

    return OutputSpec(
        path=output_path,
        report_path=report_path,
        state_path=state_path,
        work_dir=work_dir,
        sample_rate=sample_rate,
        channels=channels,
        channel_layout=layout,
        bit_depth=bit_depth,
        sample_format=sample_format,
        window_ms=window_ms,
        window_frames=window_frames,
        peak_policy=peak_policy,
        max_workers=max_workers,
        memory_limit_mb=memory_limit_mb,
        duration_ms=duration_ms,
    )


def parse_job(
    raw: dict[str, Any],
    manifest_path: Path,
    *,
    output_override: str | None = None,
    report_override: str | None = None,
    state_override: str | None = None,
    work_override: str | None = None,
    concurrency_override: int | None = None,
    memory_override: int | None = None,
    window_override: float | None = None,
) -> Job:
    manifest_dir = manifest_path.resolve().parent
    tracks_raw = raw.get("tracks")
    if not isinstance(tracks_raw, list) or not tracks_raw:
        raise BusinessError(
            "INVALID_MANIFEST",
            "清单必须包含非空 tracks 数组",
            {"field": "tracks"},
        )
    tracks = [_parse_track(item, index, manifest_dir) for index, item in enumerate(tracks_raw)]
    track_paths = [track.path for track in tracks]

    output = _parse_output(
        raw,
        manifest_dir,
        output_override=output_override,
        report_override=report_override,
        state_override=state_override,
        work_override=work_override,
        cli_base=Path.cwd(),
        concurrency_override=concurrency_override,
        memory_override=memory_override,
        window_override=window_override,
    )
    if output.path in track_paths:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "输出路径不得覆盖输入文件",
            {"output": str(output.path)},
        )
    if output.report_path in track_paths or output.state_path in track_paths:
        raise BusinessError(
            "INVALID_OUTPUT_SPEC",
            "报告或状态路径不得覆盖输入文件",
            {"report": str(output.report_path), "state": str(output.state_path)},
        )

    job = Job(manifest_path=manifest_path.resolve(), tracks=tracks, output=output, snapshot_payload={})
    return job


def validate_track_declarations(job: Job) -> None:
    """Run after probing every input; compares declared versus actual media."""
    for track in job.tracks:
        if not track.path.is_file():
            raise BusinessError(
                "INPUT_NOT_FOUND",
                "轨道文件不存在",
                {
                    "track_index": track.index,
                    "file": str(track.path),
                },
            )
        if not track.codec_name.lower().startswith("pcm_"):
            raise BusinessError(
                "UNSUPPORTED_FORMAT",
                "轨道必须是 PCM WAV",
                {
                    "track_index": track.index,
                    "codec_name": track.codec_name,
                    "file": str(track.path),
                },
            )
        if track.actual_channels not in (1, 2):
            raise BusinessError(
                "UNSUPPORTED_FORMAT",
                "仅支持单声道或立体声音轨",
                {
                    "track_index": track.index,
                    "channels": track.actual_channels,
                    "file": str(track.path),
                },
            )
        if track.actual_sample_rate != track.sample_rate:
            raise BusinessError(
                "INPUT_MISMATCH",
                "轨道声明采样率与实际 WAV 不一致",
                {
                    "track_index": track.index,
                    "declared": track.sample_rate,
                    "actual": track.actual_sample_rate,
                    "file": str(track.path),
                },
            )
        if track.actual_bit_depth != track.bit_depth:
            raise BusinessError(
                "INPUT_MISMATCH",
                "轨道声明位深与实际 WAV 不一致",
                {
                    "track_index": track.index,
                    "declared": track.bit_depth,
                    "actual": track.actual_bit_depth,
                    "file": str(track.path),
                },
            )
        if track.duration_seconds <= 0:
            raise BusinessError(
                "UNSUPPORTED_FORMAT",
                "不支持零时长音轨",
                {"track_index": track.index, "file": str(track.path)},
            )
        if track.channel_map is not None:
            if len(track.channel_map) != job.output.channels:
                raise BusinessError(
                    "INVALID_MANIFEST",
                    "channel_map 长度必须等于输出声道数",
                    {
                        "track_index": track.index,
                        "expected": job.output.channels,
                        "actual": len(track.channel_map),
                    },
                )
            for value in track.channel_map:
                if value >= track.actual_channels:
                    raise BusinessError(
                        "INVALID_MANIFEST",
                        "channel_map 引用了不存在的源声道",
                        {
                            "track_index": track.index,
                            "value": value,
                            "source_channels": track.actual_channels,
                        },
                    )
        duration_limit = track.duration_seconds * 1000.0 + 0.5
        for silence in track.silences:
            if silence.start_ms > duration_limit or silence.end_ms > duration_limit:
                raise BusinessError(
                    "INVALID_MANIFEST",
                    "静音区间超出源音轨时长",
                    {
                        "track_index": track.index,
                        "duration_ms": track.duration_seconds * 1000.0,
                        "start_ms": silence.start_ms,
                        "end_ms": silence.end_ms,
                    },
                )
        track.offset_samples = milliseconds_to_samples(
            track.offset_ms, job.output.sample_rate
        )


def compute_snapshot(job: Job) -> None:
    tracks_payload: list[dict[str, Any]] = []
    for track in job.tracks:
        tracks_payload.append(
            {
                "index": track.index,
                "path": str(track.path),
                "content_sha256": track.content_sha256,
                "sample_rate": track.sample_rate,
                "bit_depth": track.bit_depth,
                "offset_ms": track.offset_ms,
                "gain": track.gain,
                "silences": [
                    {"start_ms": silence.start_ms, "end_ms": silence.end_ms}
                    for silence in track.silences
                ],
                "channel_map": track.channel_map,
            }
        )
    output = job.output
    job.snapshot_payload = {
        "schema_version": 1,
        "tracks": tracks_payload,
        "output": {
            "sample_rate": output.sample_rate,
            "channels": output.channels,
            "channel_layout": list(output.channel_layout),
            "bit_depth": output.bit_depth,
            "sample_format": output.sample_format,
            "window_frames": output.window_frames,
            "peak_policy": output.peak_policy,
            "duration_ms": output.duration_ms,
        },
    }
    job.assign_snapshot()


def hash_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise BusinessError(
            "INPUT_NOT_FOUND",
            "无法读取轨道文件",
            {"file": str(path), "reason": str(exc)},
        ) from exc
    return digest.hexdigest()
