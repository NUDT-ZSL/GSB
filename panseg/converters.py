"""Parsers, metadata enrichment and bidirectional format conversion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_FAULT_INJECTED, E_INPUT_INVALID, PipelineError
from .models import V1, V2, InstanceRecord, PipelineConfig
from .v1_package import V1RawInstance, parse_v1_package, write_v1_package
from .v2_package import V2RawInstance, parse_v2_package, write_v2_package


@dataclass(frozen=True)
class ConversionLoss:
    instance_id: str
    field_name: str
    reason: str
    value: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "field_name": self.field_name,
            "reason": self.reason,
            "value": self.value,
        }


def load_metadata(path: str | Path) -> dict[str, dict[str, Any]]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = document.get("instances", document)
    metadata: dict[str, dict[str, Any]] = {}
    for entry in entries:
        metadata[entry["instance_id"]] = {
            "semantic_class": entry["semantic_class"],
            "confidence": float(entry["confidence"]),
            "is_crowd": bool(entry["is_crowd"]),
        }
    return metadata


def _maybe_fault(config: PipelineConfig, stage: str, instance_id: str) -> None:
    rules = config.fault_injection.get(stage)
    if not rules:
        return
    targets = rules.get("instance_ids") or []
    if instance_id in targets or rules.get("all", False):
        raise PipelineError(
            E_FAULT_INJECTED,
            f"injected failure at stage {stage} for {instance_id}",
            instance_id=instance_id,
            stage=stage,
            details={"configured_code": rules.get("code", E_FAULT_INJECTED)},
        )


def parse_v1_to_ir(
    path: str | Path,
    scene_id: str,
    metadata: dict[str, dict[str, Any]],
    config: PipelineConfig,
) -> list[InstanceRecord]:
    width, height, raw_instances = parse_v1_package(path)
    records: list[InstanceRecord] = []
    for raw in raw_instances:
        _maybe_fault(config, "parse_v1", raw.instance_id)
        meta = metadata.get(raw.instance_id)
        confidence = config.default_confidence
        is_crowd = config.default_crowd
        inferred_confidence = True
        inferred_crowd = True
        if meta is not None:
            confidence = float(meta["confidence"])
            is_crowd = bool(meta["is_crowd"])
            inferred_confidence = False
            inferred_crowd = False
        records.append(
            InstanceRecord(
                instance_id=raw.instance_id,
                scene_id=scene_id,
                semantic_class=raw.semantic_class,
                width=width,
                height=height,
                runs=raw.runs,
                source_version=V1,
                confidence=confidence,
                is_crowd=is_crowd,
                palette_name=None,
                palette_rgb=None,
                pixel_count=raw.pixel_count,
                source_flags={
                    "confidence_inferred": inferred_confidence,
                    "crowd_inferred": inferred_crowd,
                },
            )
        )
    return records


def parse_v2_to_ir(path: str | Path, scene_id: str, config: PipelineConfig) -> list[InstanceRecord]:
    width, height, raw_instances, _palette = parse_v2_package(path)
    records: list[InstanceRecord] = []
    for raw in raw_instances:
        _maybe_fault(config, "parse_v2", raw.instance_id)
        records.append(
            InstanceRecord(
                instance_id=raw.instance_id,
                scene_id=scene_id,
                semantic_class=raw.semantic_class,
                width=width,
                height=height,
                runs=raw.runs,
                source_version=V2,
                confidence=raw.confidence,
                is_crowd=raw.is_crowd,
                palette_name=raw.palette_name,
                palette_rgb=raw.palette_rgb,
                pixel_count=raw.pixel_count,
                source_flags={"confidence_inferred": False, "crowd_inferred": False},
            )
        )
    return records


def _palette_entry(record: InstanceRecord) -> tuple[str, tuple[int, int, int]]:
    if record.palette_name and record.palette_rgb:
        return record.palette_name, record.palette_rgb
    # Deterministic synthetic palette derived from the semantic class name.
    name = f"auto_{record.semantic_class}"
    digest = sum((index + 1) * ord(char) for index, char in enumerate(record.semantic_class))
    rgb = (
        (digest * 37) % 256,
        (digest * 73 + 67) % 256,
        (digest * 151 + 173) % 256,
    )
    return name, rgb


def upgrade_v1_to_v2(
    records: list[InstanceRecord],
    output_path: str | Path,
    config: PipelineConfig,
) -> list[ConversionLoss]:
    """v1 -> v2: fill confidence/crowd defaults and synthesize palette entries."""

    palette: dict[str, dict[str, Any]] = {}
    raw_instances: list[V2RawInstance] = []
    losses: list[ConversionLoss] = []
    for record in records:
        if record.source_version != V1:
            raise PipelineError(
                E_INPUT_INVALID,
                f"instance {record.instance_id} is not a v1 record",
                instance_id=record.instance_id,
            )
        _maybe_fault(config, "convert", record.instance_id)
        name, rgb = _palette_entry(record)
        palette[name] = {"rgb": list(rgb)}
        raw_instances.append(
            V2RawInstance(
                instance_id=record.instance_id,
                semantic_class=record.semantic_class,
                runs=record.runs,
                pixel_count=record.pixel_count,
                confidence=record.confidence,
                is_crowd=record.is_crowd,
                palette_name=name,
                palette_rgb=rgb,
            )
        )
        if record.source_flags.get("confidence_inferred"):
            losses.append(
                ConversionLoss(record.instance_id, "confidence", "v1 has no confidence; default inferred", record.confidence)
            )
        if record.source_flags.get("crowd_inferred"):
            losses.append(
                ConversionLoss(record.instance_id, "is_crowd", "v1 has no crowd flag; default inferred", record.is_crowd)
            )
    if records:
        write_v2_package(output_path, records[0].width, records[0].height, raw_instances, palette)
    return losses


def downgrade_v2_to_v1(
    records: list[InstanceRecord],
    output_path: str | Path,
    config: PipelineConfig,
) -> list[ConversionLoss]:
    """v2 -> v1: confidence/crowd/palette are dropped and summarized in losses."""

    raw_instances: list[V1RawInstance] = []
    losses: list[ConversionLoss] = []
    for record in records:
        if record.source_version != V2:
            raise PipelineError(
                E_INPUT_INVALID,
                f"instance {record.instance_id} is not a v2 record",
                instance_id=record.instance_id,
            )
        _maybe_fault(config, "convert", record.instance_id)
        raw_instances.append(
            V1RawInstance(
                instance_id=record.instance_id,
                semantic_class=record.semantic_class,
                runs=record.runs,
                pixel_count=record.pixel_count,
            )
        )
        losses.append(
            ConversionLoss(record.instance_id, "confidence", "not expressible in v1; dropped", record.confidence)
        )
        losses.append(
            ConversionLoss(record.instance_id, "is_crowd", "crowd semantics not expressible in v1; dropped", record.is_crowd)
        )
        losses.append(
            ConversionLoss(
                record.instance_id,
                "palette",
                "named palette not expressible in v1; dropped",
                {"name": record.palette_name, "rgb": list(record.palette_rgb or [])},
            )
        )
    if records:
        write_v1_package(output_path, records[0].width, records[0].height, raw_instances, config.rle_compression)
    return losses
