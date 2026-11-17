"""Task/frame JSON loading and deterministic validation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from . import __version__
from .color import (
    GAMUTS,
    resolve_white_point,
    canonical_gamut,
)
from .curves import (
    ALLOWED_TRANSFERS,
    PQ_CONSTANTS,
    canonical_transfer,
)
from .errors import BusinessError, require
from .tone import ALLOWED_TONE_CURVES, ToneMapper


TASK_SCHEMA = "hdr-color-task-v1"
FRAMES_SCHEMA = "hdr-color-frames-v1"
INJECTION_KINDS = ("NAN", "NEGATIVE", "PEAK_OVERFLOW", "BAD_TRANSFER")
TAIL_MODES = ("include", "drop")


@dataclass(frozen=True)
class Injection:
    frame_index: int
    kind: str
    channel: int | None = None
    value: float | None = None


@dataclass(frozen=True)
class TaskConfig:
    task_path: Path
    task_id: str
    source_transfer: str
    target_transfer: str
    source_gamut: str
    target_gamut: str
    source_white: tuple[str, tuple[float, float]]
    target_white: tuple[str, tuple[float, float]]
    reference_white_nits: float
    source_peak_nits: float
    target_peak_nits: float
    tone_mapper: ToneMapper
    window_frames: int
    tail_mode: str
    chunk_frames: int
    concurrency: int
    memory_budget_bytes: int
    curve_lut_size: int
    output_dir: Path
    frames_path: Path | None
    embedded_frames: list[list[list[float]]] | None
    injections: tuple[Injection, ...]
    parameter_summary: dict[str, Any]
    parameter_digest: str
    frames_digest: str
    version: str
    frames: list[list[list[float]]]

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def pixels_per_frame(self) -> int:
        return len(self.frames[0])

    @property
    def output_frames_path(self) -> Path:
        return self.output_dir / "converted_frames.json"

    @property
    def output_report_path(self) -> Path:
        return self.output_dir / "statistics_report.json"

    @property
    def state_path(self) -> Path:
        return self.output_dir / "run-state.json"


@dataclass(frozen=True)
class LoadedTask:
    config: TaskConfig


class _DuplicateKey(ValueError):
    pass


class _NonFiniteConstant(ValueError):
    pass


def load_task(task_path: str | Path) -> LoadedTask:
    path = Path(task_path).expanduser().resolve()
    task_raw = _read_json(path, "E_JSON_PARSE")
    if not isinstance(task_raw, dict):
        raise BusinessError(
            "E_TASK_SCHEMA", "task JSON root must be an object", {"task": str(path)}
        )
    schema = task_raw.get("schema_version", TASK_SCHEMA)
    require(
        schema == TASK_SCHEMA,
        "E_TASK_SCHEMA",
        "unsupported task schema version",
        found=schema,
        expected=TASK_SCHEMA,
    )

    task_id = task_raw.get("task_id", path.stem)
    require(
        isinstance(task_id, str) and bool(task_id.strip()),
        "E_TASK_SCHEMA",
        "task_id must be a non-empty string",
        found=repr(task_id),
    )

    source = _object(task_raw, "source", "E_TASK_SCHEMA")
    target = _object(task_raw, "target", "E_TASK_SCHEMA")
    source_transfer = canonical_transfer(source.get("transfer"))
    target_transfer = canonical_transfer(target.get("transfer"))
    source_gamut = canonical_gamut(source.get("gamut"))
    target_gamut = canonical_gamut(target.get("gamut"))

    source_default_name = _default_white_name(source_gamut)
    target_default_name = _default_white_name(target_gamut)
    source_white = resolve_white_point(
        source.get("white_point"), source_default_name
    )
    target_white = resolve_white_point(
        target.get("white_point"), target_default_name
    )

    luminance = _object(task_raw, "luminance", "E_TASK_SCHEMA")
    reference_white = _positive_number(
        luminance.get("reference_white_nits"),
        "E_LUMINANCE",
        "reference_white_nits",
    )
    source_peak = _positive_number(
        luminance.get("source_peak_nits"),
        "E_LUMINANCE",
        "source_peak_nits",
    )
    target_peak = _positive_number(
        luminance.get("target_peak_nits"),
        "E_LUMINANCE",
        "target_peak_nits",
    )
    require(
        source_peak >= reference_white,
        "E_LUMINANCE",
        "source_peak_nits must be >= reference_white_nits",
        source_peak_nits=source_peak,
        reference_white_nits=reference_white,
    )
    require(
        target_peak >= reference_white,
        "E_LUMINANCE",
        "target_peak_nits must be >= reference_white_nits",
        target_peak_nits=target_peak,
        reference_white_nits=reference_white,
    )
    if target_transfer == "PQ":
        require(
            target_peak <= PQ_CONSTANTS["peak_nits"],
            "E_LUMINANCE",
            "PQ target peak cannot exceed the ST 2084 10000 cd/m2 domain",
            target_peak_nits=target_peak,
            pq_domain_peak_nits=PQ_CONSTANTS["peak_nits"],
        )

    tone = _object(task_raw, "tone_mapping", "E_TASK_SCHEMA")
    curve = tone.get("curve")
    require(
        isinstance(curve, str) and curve in ALLOWED_TONE_CURVES,
        "E_TONE_MAPPING",
        "unsupported tone mapping curve",
        found=repr(curve),
        allowed=list(ALLOWED_TONE_CURVES),
    )
    white_nits = tone.get("white_nits", max(source_peak, target_peak))
    exposure = tone.get("exposure", 1.0)
    tone_mapper = ToneMapper.create(
        curve=curve,
        target_peak_nits=target_peak,
        white_nits=white_nits,
        exposure=exposure,
    )

    statistics = _object(task_raw, "statistics", "E_TASK_SCHEMA")
    window_frames = _positive_int(
        statistics.get("window_frames"),
        "E_STATS_CONFIG",
        "window_frames",
    )
    tail_mode = statistics.get("tail_mode", "include")
    require(
        isinstance(tail_mode, str) and tail_mode in TAIL_MODES,
        "E_STATS_CONFIG",
        "tail_mode must be include or drop",
        found=repr(tail_mode),
        allowed=list(TAIL_MODES),
    )

    processing = _object(task_raw, "processing", "E_TASK_SCHEMA")
    chunk_frames = _positive_int(
        processing.get("chunk_frames"), "E_PROCESSING_CONFIG", "chunk_frames"
    )
    concurrency = _positive_int(
        processing.get("concurrency"), "E_PROCESSING_CONFIG", "concurrency"
    )
    require(
        concurrency <= 256,
        "E_PROCESSING_CONFIG",
        "concurrency exceeds the local safety limit",
        concurrency=concurrency,
        max_concurrency=256,
    )
    memory_budget = _positive_int(
        processing.get("memory_budget_bytes"),
        "E_PROCESSING_CONFIG",
        "memory_budget_bytes",
    )
    curve_lut_size = _positive_int(
        processing.get("curve_lut_size", 65537),
        "E_PROCESSING_CONFIG",
        "curve_lut_size",
    )
    require(
        curve_lut_size >= 1025,
        "E_PROCESSING_CONFIG",
        "curve_lut_size must be at least 1025",
        curve_lut_size=curve_lut_size,
    )

    output_value = task_raw.get("output_dir")
    require(
        isinstance(output_value, str) and bool(output_value.strip()),
        "E_TASK_SCHEMA",
        "output_dir must be a non-empty string",
        found=repr(output_value),
    )
    output_dir = Path(output_value).expanduser()
    if not output_dir.is_absolute():
        output_dir = path.parent / output_dir
    output_dir = output_dir.resolve()

    frames_path: Path | None = None
    embedded_frames = None
    if "frames" in task_raw:
        embedded_frames = _validate_frames_payload(
            {"frames": task_raw["frames"]}, f"task:{path}"
        )
    else:
        frames_value = task_raw.get("frames_path")
        require(
            isinstance(frames_value, str) and bool(frames_value.strip()),
            "E_TASK_SCHEMA",
            "frames_path is required when frames are not embedded",
            found=repr(frames_value),
        )
        frames_path = Path(frames_value).expanduser()
        if not frames_path.is_absolute():
            frames_path = path.parent / frames_path
        frames_path = frames_path.resolve()
        frames_raw = _read_json(frames_path, "E_JSON_PARSE")
        frames = _validate_frames_payload(frames_raw, str(frames_path))
        embedded_frames = frames

    assert embedded_frames is not None
    injections = _validate_injections(task_raw.get("injections", []), len(embedded_frames))

    parameter_summary = {
        "engine": {
            "name": "hdr-color",
            "version": __version__,
        },
        "schema_version": TASK_SCHEMA,
        "task_id": task_id.strip(),
        "source": {
            "transfer": source_transfer,
            "gamut": source_gamut,
            "white_point": {
                "name": source_white[0],
                "xy": list(source_white[1]),
            },
        },
        "target": {
            "transfer": target_transfer,
            "gamut": target_gamut,
            "white_point": {
                "name": target_white[0],
                "xy": list(target_white[1]),
            },
        },
        "luminance": {
            "reference_white_nits": reference_white,
            "source_peak_nits": source_peak,
            "target_peak_nits": target_peak,
        },
        "tone_mapping": tone_mapper.report(),
        "statistics": {
            "window_frames": window_frames,
            "tail_mode": tail_mode,
        },
        "processing": {
            "chunk_frames": chunk_frames,
            "concurrency": concurrency,
            "memory_budget_bytes": memory_budget,
            "curve_lut_size": curve_lut_size,
        },
        "injections": [
            {
                "frame_index": item.frame_index,
                "kind": item.kind,
                "channel": item.channel,
                "value": item.value,
            }
            for item in injections
        ],
        "frames": {
            "frame_count": len(embedded_frames),
            "pixels_per_frame": len(embedded_frames[0]),
        },
    }
    parameter_digest = _digest_json(parameter_summary)
    if frames_path is not None:
        try:
            frames_digest = hashlib.sha256(frames_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise BusinessError(
                "E_IO",
                "cannot hash frames file",
                {"path": str(frames_path), "error": str(exc)},
            ) from exc
    else:
        frames_digest = _digest_json(embedded_frames)
    version = hashlib.sha256(
        f"{parameter_digest}:{frames_digest}".encode("utf-8")
    ).hexdigest()[:32]

    config = TaskConfig(
        task_path=path,
        task_id=task_id.strip(),
        source_transfer=source_transfer,
        target_transfer=target_transfer,
        source_gamut=source_gamut,
        target_gamut=target_gamut,
        source_white=source_white,
        target_white=target_white,
        reference_white_nits=reference_white,
        source_peak_nits=source_peak,
        target_peak_nits=target_peak,
        tone_mapper=tone_mapper,
        window_frames=window_frames,
        tail_mode=tail_mode,
        chunk_frames=chunk_frames,
        concurrency=concurrency,
        memory_budget_bytes=memory_budget,
        curve_lut_size=curve_lut_size,
        output_dir=output_dir,
        frames_path=frames_path,
        embedded_frames=embedded_frames,
        injections=injections,
        parameter_summary=parameter_summary,
        parameter_digest=parameter_digest,
        frames_digest=frames_digest,
        version=version,
        frames=embedded_frames,
    )
    return LoadedTask(config=config)


def _read_json(path: Path, error_code: str) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BusinessError(
            error_code,
            "cannot read JSON file",
            {"path": str(path), "error": str(exc)},
        ) from exc
    try:
        return json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_no_duplicate_keys,
        )
    except _NonFiniteConstant as exc:
        raise BusinessError(
            "E_FRAME_NONFINITE",
            "JSON contains a non-finite numeric token",
            {"path": str(path), "error": str(exc)},
        ) from exc
    except _DuplicateKey as exc:
        raise BusinessError(
            "E_JSON_PARSE",
            "JSON contains duplicate object keys",
            {"path": str(path), "error": str(exc)},
        ) from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise BusinessError(
            error_code,
            "invalid JSON",
            {"path": str(path), "error": str(exc)},
        ) from exc


def _reject_constant(value: str) -> None:
    raise _NonFiniteConstant(f"non-finite token {value}")


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate key {key!r}")
        result[key] = value
    return result


def _validate_frames_payload(payload: Any, label: str) -> list[list[list[float]]]:
    require(
        isinstance(payload, dict),
        "E_FRAMES_SCHEMA",
        "frames JSON root must be an object",
        source=label,
    )
    schema = payload.get("schema_version", FRAMES_SCHEMA)
    require(
        schema == FRAMES_SCHEMA,
        "E_FRAMES_SCHEMA",
        "unsupported frames schema version",
        source=label,
        found=schema,
        expected=FRAMES_SCHEMA,
    )
    frames_raw = payload.get("frames")
    require(
        isinstance(frames_raw, list) and bool(frames_raw),
        "E_FRAMES_SCHEMA",
        "frames must be a non-empty array",
        source=label,
    )
    frames: list[list[list[float]]] = []
    expected_width: int | None = None
    for frame_index, frame in enumerate(frames_raw):
        require(
            isinstance(frame, list) and bool(frame),
            "E_FRAMES_SCHEMA",
            "each frame must be a non-empty pixel array",
            source=label,
            frame_index=frame_index,
        )
        if expected_width is None:
            expected_width = len(frame)
        require(
            len(frame) == expected_width,
            "E_FRAME_WIDTH",
            "all frames must have the same pixel count",
            source=label,
            frame_index=frame_index,
            expected=expected_width,
            found=len(frame),
        )
        pixels: list[list[float]] = []
        for pixel_index, pixel in enumerate(frame):
            require(
                isinstance(pixel, list) and len(pixel) == 3,
                "E_FRAME_WIDTH",
                "each pixel must contain exactly three channels",
                source=label,
                frame_index=frame_index,
                pixel_index=pixel_index,
                found=len(pixel) if isinstance(pixel, list) else type(pixel).__name__,
            )
            channels: list[float] = []
            for channel_index, value in enumerate(pixel):
                if not _number(value):
                    raise BusinessError(
                        "E_FRAME_NONFINITE",
                        "frame channels must be finite numbers",
                        {
                            "source": label,
                            "frame_index": frame_index,
                            "pixel_index": pixel_index,
                            "channel_index": channel_index,
                            "value": repr(value),
                        },
                    )
                channel = float(value)
                if channel < 0.0:
                    raise BusinessError(
                        "E_FRAME_NEGATIVE",
                        "input linear-light channels must be non-negative",
                        {
                            "source": label,
                            "frame_index": frame_index,
                            "pixel_index": pixel_index,
                            "channel_index": channel_index,
                            "value": channel,
                        },
                    )
                channels.append(channel)
            pixels.append(channels)
        frames.append(pixels)
    return frames


def _validate_injections(value: Any, frame_count: int) -> tuple[Injection, ...]:
    require(
        isinstance(value, list),
        "E_INJECTION_SCHEMA",
        "injections must be an array",
        found=type(value).__name__,
    )
    result: list[Injection] = []
    for index, item in enumerate(value):
        require(
            isinstance(item, dict),
            "E_INJECTION_SCHEMA",
            "each injection must be an object",
            injection_index=index,
        )
        frame_index = _nonnegative_int(
            item.get("frame_index"), "E_INJECTION_SCHEMA", "frame_index"
        )
        require(
            frame_index < frame_count,
            "E_INJECTION_SCHEMA",
            "injection frame_index is outside the frame sequence",
            injection_index=index,
            frame_index=frame_index,
            frame_count=frame_count,
        )
        kind_value = item.get("kind")
        if not isinstance(kind_value, str):
            raise BusinessError(
                "E_INJECTION_SCHEMA",
                "injection kind must be a string",
                {"injection_index": index, "kind": repr(kind_value)},
            )
        kind = kind_value.strip().upper()
        require(
            kind in INJECTION_KINDS,
            "E_INJECTION_SCHEMA",
            "unsupported injection kind",
            injection_index=index,
            kind=kind_value,
            allowed=list(INJECTION_KINDS),
        )
        channel: int | None = None
        if kind in ("NAN", "NEGATIVE"):
            channel_value = item.get("channel", 0)
            channel = _nonnegative_int(
                channel_value, "E_INJECTION_SCHEMA", "channel"
            )
            require(
                channel <= 2,
                "E_INJECTION_SCHEMA",
                "injection channel must be 0, 1 or 2",
                injection_index=index,
                channel=channel,
            )
        raw_value = item.get("value")
        if raw_value is None:
            injection_value = None
        elif _number(raw_value) and float(raw_value) > 0.0:
            injection_value = float(raw_value)
        else:
            raise BusinessError(
                "E_INJECTION_SCHEMA",
                "injection value must be finite and positive when supplied",
                {"injection_index": index, "value": repr(raw_value)},
            )
        result.append(
            Injection(
                frame_index=frame_index,
                kind=kind,
                channel=channel,
                value=injection_value,
            )
        )
    return tuple(result)


def _default_white_name(gamut_name: str) -> str:
    native = GAMUTS[gamut_name].white
    for name, xy in (
        ("D65", (0.3127, 0.3290)),
        ("D60", (0.32168, 0.33767)),
        ("D50", (0.3457, 0.3585)),
    ):
        if native == xy:
            return name
    return "NATIVE"


def _object(parent: dict[str, Any], key: str, code: str) -> dict[str, Any]:
    value = parent.get(key)
    require(
        isinstance(value, dict),
        code,
        f"{key} must be an object",
        field=key,
        found=type(value).__name__,
    )
    return value


def _positive_number(value: Any, code: str, field: str) -> float:
    if not _number(value) or float(value) <= 0.0:
        raise BusinessError(
            code,
            f"{field} must be finite and positive",
            {field: repr(value)},
        )
    return float(value)


def _positive_int(value: Any, code: str, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value <= 0
    ):
        raise BusinessError(
            code,
            f"{field} must be a positive integer",
            {field: repr(value)},
        )
    return int(value)


def _nonnegative_int(value: Any, code: str, field: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
    ):
        raise BusinessError(
            code,
            f"{field} must be a non-negative integer",
            {field: repr(value)},
        )
    return int(value)


def _number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _digest_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
