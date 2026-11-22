from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .calibration import CalibrationStats
from .errors import (
    DiagnosticError,
    E_VERSION_UNSUPPORTED,
    format_error,
)
from .model import LAYER_ORDER
from .utils import require_bool, require_finite_float, require_int, sha256_json


V1_FIELDS = ("layer", "scale", "zero_point")
V2_FIELDS = (
    "layer",
    "weight_scale",
    "zero_point",
    "per_channel",
    "channel_scales",
    "calibration_samples",
)
SUPPORTED_VERSIONS = {"v1", "v2", "v3"}
DYNAMIC_MODES = {"none", "per_activation"}


@dataclass(frozen=True)
class LayerQuant:
    name: str
    weight_scale: float
    zero_point: int
    per_channel: bool = False
    channel_scales: tuple[float, ...] = ()
    calibration_samples: int = 0
    dynamic_range_mode: str = "none"
    quant_version: str = ""
    calibration_version: str = ""

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight_scale": self.weight_scale,
            "zero_point": self.zero_point,
            "per_channel": self.per_channel,
            "channel_scales": list(self.channel_scales),
            "calibration_samples": self.calibration_samples,
            "dynamic_range_mode": self.dynamic_range_mode,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.semantic_dict(),
            "quant_version": self.quant_version,
            "calibration_version": self.calibration_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LayerQuant":
        mode = str(payload.get("dynamic_range_mode", "none"))
        if mode not in DYNAMIC_MODES:
            raise ValueError(f"unsupported dynamic range mode: {mode}")
        return cls(
            name=str(payload["name"]),
            weight_scale=float(payload["weight_scale"]),
            zero_point=int(payload["zero_point"]),
            per_channel=bool(payload.get("per_channel", False)),
            channel_scales=tuple(float(v) for v in payload.get("channel_scales", ())),
            calibration_samples=int(payload.get("calibration_samples", 0)),
            dynamic_range_mode=mode,
            quant_version=str(payload.get("quant_version", "")),
            calibration_version=str(payload.get("calibration_version", "")),
        )

    def quant_fingerprint(self) -> str:
        payload = self.semantic_dict()
        payload.pop("calibration_samples", None)
        return sha256_json(payload)

    def versions_with_calibration(self, calibration: CalibrationStats) -> "LayerQuant":
        return replace(
            self,
            quant_version="q-" + self.quant_fingerprint()[:16],
            calibration_version=calibration.layer_version(self.name),
            calibration_samples=calibration.sample_count_for(self.name),
        )


@dataclass(frozen=True)
class FillRecord:
    layer: str
    field: str
    value: str
    source: str

    def to_dict(self) -> dict[str, str]:
        return {
            "layer": self.layer,
            "field": self.field,
            "value": self.value,
            "source": self.source,
        }


@dataclass(frozen=True)
class DropRecord:
    layer: str
    field: str
    old_value: str
    rule: str
    semantic_loss: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "field": self.field,
            "old_value": self.old_value,
            "rule": self.rule,
            "semantic_loss": self.semantic_loss,
        }


@dataclass(frozen=True)
class ParamSet:
    format_version: str
    layers: tuple[LayerQuant, ...]
    fills: tuple[FillRecord, ...] = ()
    drops: tuple[DropRecord, ...] = ()
    source: str = ""

    def layer(self, name: str) -> LayerQuant:
        for item in self.layers:
            if item.name == name:
                return item
        raise KeyError(name)

    def content_checksum(self) -> str:
        return sha256_json(
            {
                "format_version": self.format_version,
                "layers": [layer.semantic_dict() for layer in self.layers],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "layers": [layer.to_dict() for layer in self.layers],
            "fills": [item.to_dict() for item in self.fills],
            "drops": [item.to_dict() for item in self.drops],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ParamSet":
        return cls(
            format_version=str(payload["format_version"]),
            layers=tuple(LayerQuant.from_dict(item) for item in payload["layers"]),
            fills=tuple(FillRecord(**item) for item in payload.get("fills", ())),
            drops=tuple(DropRecord(**item) for item in payload.get("drops", ())),
            source=str(payload.get("source", "")),
        )


@dataclass(frozen=True)
class ParseResult:
    params: ParamSet
    calibration: CalibrationStats | None = None


@dataclass(frozen=True)
class MigrationResult:
    params: ParamSet
    fills: tuple[FillRecord, ...] = ()
    drops: tuple[DropRecord, ...] = ()

    @property
    def semantic_loss(self) -> bool:
        return any(item.semantic_loss for item in self.drops)


def parse_quant_params(
    payload: str | bytes, *, source: str = "<memory>"
) -> ParseResult:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise format_error(
                format_version="unknown",
                field="encoding",
                message="quantization parameter input is not valid UTF-8",
            ) from exc
    else:
        text = payload
    stripped = text.lstrip("\ufeff \t\r\n")
    if not stripped:
        raise format_error(
            format_version="unknown",
            field="version",
            message="empty quantization parameter input (truncated header)",
        )
    if stripped.startswith("{"):
        return _parse_v3(stripped, source=source)
    return _parse_text(text, source=source)


def parse_quant_file(path: str | Path) -> ParseResult:
    file_path = Path(path)
    return parse_quant_params(file_path.read_bytes(), source=str(file_path))


def _parse_text(text: str, *, source: str) -> ParseResult:
    rows: list[tuple[int, str]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        rows.append((line_number, line))
    if not rows:
        raise format_error(
            format_version="unknown",
            field="version",
            message="header is missing or truncated",
        )

    header_line_number, first = rows[0]
    version: str | None = None
    data_start = 0
    if first.lower().startswith("version"):
        if "=" not in first:
            raise format_error(
                format_version="unknown",
                field="version",
                message="truncated version header; expected version=v1|v2",
                line=header_line_number,
            )
        key, raw_version = first.split("=", 1)
        if key.strip().lower() != "version":
            raise format_error(
                format_version="unknown",
                field="version",
                message=f"unexpected header key {key!r}",
                line=header_line_number,
            )
        raw_version = raw_version.strip()
        if raw_version.lower() in {"v", "v0", ""}:
            raise format_error(
                format_version=raw_version or "unknown",
                field="version",
                message="truncated version header",
                line=header_line_number,
            )
        if raw_version not in SUPPORTED_VERSIONS:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                f"unsupported quantization parameter version {raw_version!r}",
                format_version=raw_version,
                field="version",
            )
        if raw_version == "v3":
            raise format_error(
                format_version="v3",
                field="format_version",
                message="v3 is a nested JSON format; text rows are not valid v3",
                line=header_line_number,
            )
        version = raw_version
        data_start = 1
    elif first.lower().startswith("fields="):
        version = _infer_text_version_from_fields(first.split("=", 1)[1])
        data_start = 0
    elif _header_fields(first) == V1_FIELDS:
        version = "v1"
        data_start = 1
    elif _header_fields(first) == V2_FIELDS:
        version = "v2"
        data_start = 1
    elif "," in first and len(_split_row(first)) in {3, len(V2_FIELDS)}:
        version = "v1" if len(_split_row(first)) == 3 else "v2"
        data_start = 0
    else:
        raise format_error(
            format_version="unknown",
            field="version",
            message="header is missing or truncated; no recognizable version or field set",
            line=header_line_number,
        )

    remaining = rows[data_start:]
    if remaining and remaining[0][1].lower().startswith("fields="):
        declared = _parse_field_declaration(
            remaining[0][1], version=version, line_number=remaining[0][0]
        )
        expected_fields = declared
        remaining = remaining[1:]
    elif remaining and tuple(_split_row(remaining[0][1])) in (V1_FIELDS, V2_FIELDS):
        expected_fields = tuple(_split_row(remaining[0][1]))
        if version == "v1" and expected_fields != V1_FIELDS:
            raise format_error(
                format_version="v1",
                field="fields",
                message="v1 field set must be exactly layer,scale,zero_point",
            )
        remaining = remaining[1:]
    else:
        expected_fields = V1_FIELDS if version == "v1" else V2_FIELDS

    _validate_required_fields(expected_fields, version)
    if version == "v1":
        return ParseResult(_parse_v1_rows(remaining, expected_fields, source=source))
    return ParseResult(_parse_v2_rows(remaining, expected_fields, source=source))


def _infer_text_version_from_fields(raw_fields: str) -> str:
    fields = tuple(part.strip() for part in raw_fields.split(","))
    if fields == V1_FIELDS:
        return "v1"
    if tuple(field for field in V2_FIELDS if field in fields) == V2_FIELDS:
        return "v2"
    raise format_error(
        format_version="unknown",
        field="fields",
        message=f"unrecognized field set: {raw_fields!r}",
    )


def _header_fields(line: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in _split_row(line))


def _split_row(line: str) -> list[str]:
    return [part.strip() for part in line.split(",")]


def _parse_field_declaration(
    declaration: str, *, version: str, line_number: int
) -> tuple[str, ...]:
    _, raw_fields = declaration.split("=", 1)
    fields = tuple(part.strip() for part in raw_fields.split(","))
    if not fields or any(not field for field in fields):
        raise format_error(
            format_version=version,
            field="fields",
            message="field declaration contains an empty field name",
            line=line_number,
        )
    return fields


def _validate_required_fields(fields: tuple[str, ...], version: str) -> None:
    required = V1_FIELDS if version == "v1" else V2_FIELDS
    for field in required:
        if field not in fields:
            raise format_error(
                format_version=version,
                field=field,
                message=f"required field {field!r} is missing from the field declaration",
            )
    if version == "v1" and fields != V1_FIELDS:
        raise format_error(
            format_version="v1",
            field="fields",
            message="v1 field set must be exactly layer,scale,zero_point",
        )


def _row_columns(
    columns: list[str], expected_fields: tuple[str, ...], *, version: str, line: int
) -> dict[str, str]:
    if len(columns) != len(expected_fields):
        if len(columns) < len(expected_fields):
            field = expected_fields[len(columns)]
            message = f"row has {len(columns)} fields; expected field {field!r}"
        else:
            field = "<extra>"
            message = f"row has {len(columns)} fields; expected {len(expected_fields)}"
        raise format_error(
            format_version=version,
            field=field,
            message=message,
            line=line,
        )
    return dict(zip(expected_fields, columns))


def _parse_v1_rows(
    rows: list[tuple[int, str]], fields: tuple[str, ...], *, source: str
) -> ParamSet:
    layers: list[LayerQuant] = []
    seen: set[str] = set()
    for line_number, line in rows:
        values = _row_columns(_split_row(line), fields, version="v1", line=line_number)
        name = values["layer"].strip()
        if not name:
            raise format_error(
                format_version="v1",
                field="layer",
                message="layer name cannot be empty",
                line=line_number,
            )
        if name in seen:
            raise format_error(
                format_version="v1",
                field="layer",
                message=f"duplicate layer {name!r}",
                line=line_number,
            )
        seen.add(name)
        scale = require_finite_float(values["scale"], version="v1", field="scale")
        if scale <= 0:
            raise format_error(
                format_version="v1",
                field="scale",
                message="scale must be positive",
                line=line_number,
            )
        zero_point = require_int(
            values["zero_point"], version="v1", field="zero_point"
        )
        layers.append(
            LayerQuant(
                name=name,
                weight_scale=scale,
                zero_point=zero_point,
                calibration_samples=0,
                dynamic_range_mode="none",
            )
        )
    if not layers:
        raise format_error(
            format_version="v1",
            field="layer",
            message="v1 parameter table contains no data rows",
        )
    return _ordered_paramset("v1", layers, source=source)


def _parse_v2_rows(
    rows: list[tuple[int, str]], fields: tuple[str, ...], *, source: str
) -> ParamSet:
    layers: list[LayerQuant] = []
    seen: set[str] = set()
    for line_number, line in rows:
        values = _row_columns(_split_row(line), fields, version="v2", line=line_number)
        name = values["layer"].strip()
        if not name:
            raise format_error(
                format_version="v2",
                field="layer",
                message="layer name cannot be empty",
                line=line_number,
            )
        if name in seen:
            raise format_error(
                format_version="v2",
                field="layer",
                message=f"duplicate layer {name!r}",
                line=line_number,
            )
        seen.add(name)
        scale = require_finite_float(
            values["weight_scale"], version="v2", field="weight_scale"
        )
        if scale <= 0:
            raise format_error(
                format_version="v2",
                field="weight_scale",
                message="weight_scale must be positive",
                line=line_number,
            )
        zero_point = require_int(
            values["zero_point"], version="v2", field="zero_point"
        )
        per_channel = require_bool(
            values["per_channel"], version="v2", field="per_channel"
        )
        raw_scales = values["channel_scales"].strip()
        channel_scales = tuple(
            require_finite_float(part, version="v2", field="channel_scales")
            for part in raw_scales.split("|")
            if part.strip()
        )
        if per_channel and not channel_scales:
            raise format_error(
                format_version="v2",
                field="channel_scales",
                message="per-channel quantization requires non-empty channel_scales",
                line=line_number,
            )
        if not per_channel and channel_scales:
            raise format_error(
                format_version="v2",
                field="channel_scales",
                message="channel_scales must be empty when per_channel is false",
                line=line_number,
            )
        if any(value <= 0 for value in channel_scales):
            raise format_error(
                format_version="v2",
                field="channel_scales",
                message="channel scales must be positive",
                line=line_number,
            )
        samples = require_int(
            values["calibration_samples"],
            version="v2",
            field="calibration_samples",
        )
        if samples < 0:
            raise format_error(
                format_version="v2",
                field="calibration_samples",
                message="calibration_samples must be non-negative",
                line=line_number,
            )
        layers.append(
            LayerQuant(
                name=name,
                weight_scale=scale,
                zero_point=zero_point,
                per_channel=per_channel,
                channel_scales=channel_scales,
                calibration_samples=samples,
                dynamic_range_mode="none",
            )
        )
    if not layers:
        raise format_error(
            format_version="v2",
            field="layer",
            message="v2 parameter table contains no data rows",
        )
    return _ordered_paramset("v2", layers, source=source)


def _ordered_paramset(
    version: str,
    layers: list[LayerQuant],
    *,
    source: str,
    fills: tuple[FillRecord, ...] = (),
    drops: tuple[DropRecord, ...] = (),
) -> ParamSet:
    by_name = {item.name: item for item in layers}
    ordered = [by_name[name] for name in LAYER_ORDER if name in by_name]
    ordered.extend(item for item in layers if item.name not in LAYER_ORDER)
    return ParamSet(
        format_version=version,
        layers=tuple(ordered),
        fills=fills,
        drops=drops,
        source=source,
    )


def _parse_v3(text: str, *, source: str) -> ParseResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise format_error(
            format_version="v3",
            field="format_version",
            message=f"truncated or invalid v3 JSON: {exc.msg}",
        ) from exc
    if not isinstance(payload, dict):
        raise format_error(
            format_version="v3",
            field="<root>",
            message="v3 root must be a JSON object",
        )
    raw_version = payload.get("format_version")
    if raw_version is None:
        if "layers" in payload or "calibration" in payload:
            raise format_error(
                format_version="v3",
                field="format_version",
                message="required field format_version is missing",
            )
        raise format_error(
            format_version="unknown",
            field="format_version",
            message="version header is missing (truncated v3 object)",
        )
    version = str(raw_version)
    if version not in SUPPORTED_VERSIONS:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            f"unsupported quantization parameter version {version!r}",
            format_version=version,
            field="format_version",
        )
    if version != "v3":
        raise format_error(
            format_version=version,
            field="format_version",
            message=f"JSON transport is only valid for v3, got {version}",
        )

    raw_layers = payload.get("layers")
    if raw_layers is None:
        raise format_error(
            format_version="v3",
            field="layers",
            message="required field 'layers' is missing",
        )
    if isinstance(raw_layers, dict):
        layer_items = list(raw_layers.items())
    elif isinstance(raw_layers, list):
        layer_items = []
        for index, item in enumerate(raw_layers):
            if not isinstance(item, dict) or "name" not in item:
                raise format_error(
                    format_version="v3",
                    field=f"layers[{index}].name",
                    message="v3 layer entry must be an object with a name",
                )
            layer_items.append((str(item["name"]), item))
    else:
        raise format_error(
            format_version="v3",
            field="layers",
            message="field 'layers' must be an object or array",
        )
    if not layer_items:
        raise format_error(
            format_version="v3",
            field="layers",
            message="field 'layers' cannot be empty",
        )

    calibration_payload = payload.get("calibration")
    if not isinstance(calibration_payload, dict):
        raise format_error(
            format_version="v3",
            field="calibration",
            message="required field 'calibration' must be an object",
        )
    calibration = _parse_v3_calibration(calibration_payload)

    layers: list[LayerQuant] = []
    seen: set[str] = set()
    for name, raw_layer in layer_items:
        if not isinstance(raw_layer, dict):
            raise format_error(
                format_version="v3",
                field=f"layers.{name}",
                message="layer entry must be an object",
            )
        if name in seen:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}",
                message=f"duplicate layer {name!r}",
            )
        seen.add(name)
        if "weight" not in raw_layer or not isinstance(raw_layer["weight"], dict):
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.weight",
                message="required object field 'weight' is missing",
            )
        weight = raw_layer["weight"]
        if "scale" not in weight:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.weight.scale",
                message="required field 'scale' is missing",
            )
        if "zero_point" not in weight:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.weight.zero_point",
                message="required field 'zero_point' is missing",
            )
        scale = require_finite_float(
            weight["scale"], version="v3", field=f"layers.{name}.weight.scale"
        )
        if scale <= 0:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.weight.scale",
                message="scale must be positive",
            )
        zero_point = require_int(
            weight["zero_point"],
            version="v3",
            field=f"layers.{name}.weight.zero_point",
        )
        per_channel = require_bool(
            raw_layer.get("per_channel", False),
            version="v3",
            field=f"layers.{name}.per_channel",
        )
        raw_channel_scales = raw_layer.get("channel_scales", [])
        if not isinstance(raw_channel_scales, list):
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.channel_scales",
                message="channel_scales must be an array",
            )
        channel_scales = tuple(
            require_finite_float(
                value,
                version="v3",
                field=f"layers.{name}.channel_scales",
            )
            for value in raw_channel_scales
        )
        if per_channel and not channel_scales:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.channel_scales",
                message="per-channel quantization requires channel_scales",
            )
        if not per_channel and channel_scales:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.channel_scales",
                message="channel_scales must be empty when per_channel is false",
            )
        if any(value <= 0 for value in channel_scales):
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.channel_scales",
                message="channel scales must be positive",
            )

        dynamic = raw_layer.get("dynamic_range")
        if not isinstance(dynamic, dict):
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.dynamic_range",
                message="required object field 'dynamic_range' is missing",
            )
        mode = dynamic.get("mode")
        if mode is None:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.dynamic_range.mode",
                message="required field 'mode' is missing",
            )
        mode = str(mode)
        if mode not in DYNAMIC_MODES:
            raise format_error(
                format_version="v3",
                field=f"layers.{name}.dynamic_range.mode",
                message=f"unsupported dynamic range mode {mode!r}",
            )
        layer_calibration = calibration.layer(name)
        samples = layer_calibration.samples if layer_calibration else 0
        if mode == "per_activation" and (
            layer_calibration is None or layer_calibration.samples <= 0
        ):
            raise format_error(
                format_version="v3",
                field=f"calibration.layers.{name}",
                message="per_activation dynamic range needs calibrated activation statistics",
            )
        layers.append(
            LayerQuant(
                name=name,
                weight_scale=scale,
                zero_point=zero_point,
                per_channel=per_channel,
                channel_scales=channel_scales,
                calibration_samples=samples,
                dynamic_range_mode=mode,
            )
        )
    return ParseResult(
        params=_ordered_paramset("v3", layers, source=source),
        calibration=calibration,
    )


def _parse_v3_calibration(payload: dict[str, Any]) -> CalibrationStats:
    from .calibration import LayerCalibration

    for field in ("version", "samples", "layers"):
        if field not in payload:
            raise format_error(
                format_version="v3",
                field=f"calibration.{field}",
                message=f"required field 'calibration.{field}' is missing",
            )
    version = str(payload["version"])
    samples = require_int(
        payload["samples"], version="v3", field="calibration.samples"
    )
    if samples < 0:
        raise format_error(
            format_version="v3",
            field="calibration.samples",
            message="calibration.samples must be non-negative",
        )
    raw_layers = payload["layers"]
    if not isinstance(raw_layers, dict):
        raise format_error(
            format_version="v3",
            field="calibration.layers",
            message="calibration.layers must be an object",
        )
    layer_items: list[LayerCalibration] = []
    for name, raw in raw_layers.items():
        if not isinstance(raw, dict):
            raise format_error(
                format_version="v3",
                field=f"calibration.layers.{name}",
                message="calibration layer must be an object",
            )
        for field in ("min", "max", "samples", "version"):
            if field not in raw:
                raise format_error(
                    format_version="v3",
                    field=f"calibration.layers.{name}.{field}",
                    message=f"required field {field!r} is missing",
                )
        minimum = require_finite_float(
            raw["min"], version="v3", field=f"calibration.layers.{name}.min"
        )
        maximum = require_finite_float(
            raw["max"], version="v3", field=f"calibration.layers.{name}.max"
        )
        layer_samples = require_int(
            raw["samples"],
            version="v3",
            field=f"calibration.layers.{name}.samples",
        )
        if maximum < minimum:
            raise format_error(
                format_version="v3",
                field=f"calibration.layers.{name}.max",
                message="calibration max must be >= min",
            )
        layer_items.append(
            LayerCalibration(
                name=name,
                minimum=minimum,
                maximum=maximum,
                samples=layer_samples,
                source_hash=str(raw.get("source_hash", payload.get("source_hash", ""))),
                version=str(raw["version"]),
            )
        )
    return CalibrationStats(
        version=version,
        source_hash=str(payload.get("source_hash", "")),
        sample_count=samples,
        layers=tuple(layer_items),
    )


def serialize_quant_params(
    params: ParamSet,
    *,
    version: str | None = None,
    calibration: CalibrationStats | None = None,
) -> str:
    target = version or params.format_version
    ordered = [params.layer(name) for name in LAYER_ORDER if any(x.name == name for x in params.layers)]
    ordered.extend(item for item in params.layers if item.name not in LAYER_ORDER)
    if target == "v1":
        lines = ["version=v1", ",".join(V1_FIELDS)]
        for layer in ordered:
            lines.append(f"{layer.name},{layer.weight_scale:.17g},{layer.zero_point}")
        return "\n".join(lines) + "\n"
    if target == "v2":
        lines = ["version=v2", "fields=" + ",".join(V2_FIELDS)]
        for layer in ordered:
            channel_scales = "|".join(f"{value:.17g}" for value in layer.channel_scales)
            lines.append(
                ",".join(
                    [
                        layer.name,
                        f"{layer.weight_scale:.17g}",
                        str(layer.zero_point),
                        "true" if layer.per_channel else "false",
                        channel_scales,
                        str(layer.calibration_samples),
                    ]
                )
            )
        return "\n".join(lines) + "\n"
    if target == "v3":
        if calibration is None:
            raise ValueError("v3 serialization requires calibration statistics")
        payload = {
            "format_version": "v3",
            "network": "deterministic-gray-cnn-16",
            "calibration": calibration.to_dict(),
            "layers": {
                layer.name: {
                    "weight": {
                        "scale": layer.weight_scale,
                        "zero_point": layer.zero_point,
                    },
                    "per_channel": layer.per_channel,
                    "channel_scales": list(layer.channel_scales),
                    "dynamic_range": {
                        "mode": layer.dynamic_range_mode,
                        "range_ref": (
                            f"calibration.layers.{layer.name}"
                            if layer.dynamic_range_mode == "per_activation"
                            else None
                        ),
                    },
                }
                for layer in ordered
            },
        }
        return json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    raise DiagnosticError(
        E_VERSION_UNSUPPORTED,
        f"cannot serialize unsupported version {target!r}",
        format_version=target,
        field="version",
    )


def upgrade_v1_to_v2(params: ParamSet) -> MigrationResult:
    if params.format_version != "v1":
        raise ValueError("upgrade_v1_to_v2 expects a v1 parameter set")
    layers: list[LayerQuant] = []
    fills: list[FillRecord] = []
    for layer in params.layers:
        layers.append(
            LayerQuant(
                name=layer.name,
                weight_scale=layer.weight_scale,
                zero_point=layer.zero_point,
                per_channel=False,
                channel_scales=(),
                calibration_samples=0,
                dynamic_range_mode="none",
            )
        )
        fills.extend(
            [
                FillRecord(
                    layer.name,
                    "per_channel",
                    "false",
                    "v1-compat: v1 has one flat scale per layer",
                ),
                FillRecord(
                    layer.name,
                    "channel_scales",
                    "[]",
                    "v1-compat: no channel dimension was expressed",
                ),
                FillRecord(
                    layer.name,
                    "calibration_samples",
                    "0",
                    "v1-compat: no calibration binding was expressed",
                ),
                FillRecord(
                    layer.name,
                    "dynamic_range_mode",
                    "none",
                    "v1-compat: v1 only supports static quantization",
                ),
            ]
        )
    upgraded = _ordered_paramset(
        "v2", layers, source=params.source, fills=tuple(fills)
    )
    return MigrationResult(params=upgraded, fills=tuple(fills))


def upgrade_v2_to_v3(
    params: ParamSet, calibration: CalibrationStats | None = None
) -> MigrationResult:
    if params.format_version != "v2":
        raise ValueError("upgrade_v2_to_v3 expects a v2 parameter set")
    layers: list[LayerQuant] = []
    fills: list[FillRecord] = []
    for layer in params.layers:
        samples = (
            calibration.sample_count_for(layer.name)
            if calibration is not None
            else layer.calibration_samples
        )
        layers.append(
            LayerQuant(
                name=layer.name,
                weight_scale=layer.weight_scale,
                zero_point=layer.zero_point,
                per_channel=layer.per_channel,
                channel_scales=layer.channel_scales,
                calibration_samples=samples,
                dynamic_range_mode="none",
            )
        )
        fills.append(
            FillRecord(
                layer.name,
                "dynamic_range_mode",
                "none",
                "v2-compat: static range retained; per-activation dynamic mode not declared in v2",
            )
        )
        if calibration is None:
            fills.append(
                FillRecord(
                    layer.name,
                    "calibration_binding",
                    str(layer.calibration_samples),
                    "v2-compat: row sample count copied into v3 calibration binding",
                )
            )
        else:
            fills.append(
                FillRecord(
                    layer.name,
                    "calibration_binding",
                    calibration.layer_version(layer.name),
                    "local-calibration: bound current calibration statistics version",
                )
            )
    upgraded = _ordered_paramset(
        "v3", layers, source=params.source, fills=tuple(fills)
    )
    return MigrationResult(params=upgraded, fills=tuple(fills))


def downgrade_v3_to_v1(
    params: ParamSet, *, channel_flatten_rule: str = "mean"
) -> MigrationResult:
    if params.format_version != "v3":
        raise ValueError("downgrade_v3_to_v1 expects a v3 parameter set")
    if channel_flatten_rule not in {"mean", "median", "max", "first"}:
        raise ValueError("channel_flatten_rule must be mean, median, max, or first")
    layers: list[LayerQuant] = []
    drops: list[DropRecord] = []
    for layer in params.layers:
        if layer.channel_scales:
            if channel_flatten_rule == "mean":
                flattened = float(sum(layer.channel_scales) / len(layer.channel_scales))
            elif channel_flatten_rule == "median":
                sorted_scales = sorted(layer.channel_scales)
                middle = len(sorted_scales) // 2
                if len(sorted_scales) % 2:
                    flattened = float(sorted_scales[middle])
                else:
                    flattened = float(
                        (sorted_scales[middle - 1] + sorted_scales[middle]) / 2.0
                    )
            elif channel_flatten_rule == "max":
                flattened = float(max(layer.channel_scales))
            else:
                flattened = float(layer.channel_scales[0])
        else:
            flattened = layer.weight_scale

        scales_equal = all(
            abs(value - flattened) <= 1e-15 for value in layer.channel_scales
        )
        if layer.per_channel:
            drops.append(
                DropRecord(
                    layer.name,
                    "per_channel",
                    "true",
                    f"flattened to one scale using {channel_flatten_rule}",
                    semantic_loss=not scales_equal,
                )
            )
        if layer.channel_scales:
            drops.append(
                DropRecord(
                    layer.name,
                    "channel_scales",
                    "|".join(f"{value:.17g}" for value in layer.channel_scales),
                    f"flattened using {channel_flatten_rule}",
                    semantic_loss=not scales_equal,
                )
            )
        if layer.dynamic_range_mode != "none":
            drops.append(
                DropRecord(
                    layer.name,
                    "dynamic_range_mode",
                    layer.dynamic_range_mode,
                    "dropped: v1 has no dynamic activation range field",
                    semantic_loss=True,
                )
            )
        if layer.calibration_samples:
            drops.append(
                DropRecord(
                    layer.name,
                    "calibration_samples",
                    str(layer.calibration_samples),
                    "dropped: v1 has no calibration binding field",
                    semantic_loss=False,
                )
            )
        layers.append(
            LayerQuant(
                name=layer.name,
                weight_scale=flattened,
                zero_point=layer.zero_point,
                per_channel=False,
                channel_scales=(),
                calibration_samples=0,
                dynamic_range_mode="none",
            )
        )
    downgraded = _ordered_paramset(
        "v1", layers, source=params.source, drops=tuple(drops)
    )
    return MigrationResult(params=downgraded, drops=tuple(drops))
