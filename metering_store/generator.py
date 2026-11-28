"""Deterministic synthetic metering records and report event streams."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import parse_record_stream, serialize_records

BASE_TS_MS = 1_700_000_000_000
SAMPLE_FORMAT = "metering-store-demo-v1"


@dataclass(frozen=True)
class AreaSpec:
    area_id: str
    meters: tuple[str, ...]
    collection_interval_ms: int
    batch_window_records: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "area_id": self.area_id,
            "meters": list(self.meters),
            "collection_interval_ms": self.collection_interval_ms,
            "batch_window_records": self.batch_window_records,
            "batch_window_ms": self.collection_interval_ms * self.batch_window_records,
        }


@dataclass(frozen=True)
class SampleFile:
    file_id: str
    area_id: str
    window_index: int
    data: bytes
    record_count: int
    derivation: str = "base"
    source_file_id: str | None = None
    mutation: dict[str, Any] | None = None

    @property
    def payload_path(self) -> str:
        return f"files/{self.file_id}.jsonl"

    def to_manifest(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "file_id": self.file_id,
            "area_id": self.area_id,
            "window_index": self.window_index,
            "payload": self.payload_path,
            "size_bytes": len(self.data),
            "record_count": self.record_count,
            "sha256": hashlib.sha256(self.data).hexdigest(),
            "derivation": self.derivation,
        }
        if self.source_file_id is not None:
            meta["source_file_id"] = self.source_file_id
        if self.mutation is not None:
            meta["mutation"] = self.mutation
        return meta


@dataclass(frozen=True)
class SampleEvent:
    seq: int
    logical_time: int
    kind: str
    file_id: str | None = None
    source_file_id: str | None = None
    policy_index: int | None = None

    def to_dict(self) -> dict[str, Any]:
        event: dict[str, Any] = {
            "seq": self.seq,
            "logical_time": self.logical_time,
            "kind": self.kind,
        }
        if self.file_id is not None:
            event["file_id"] = self.file_id
        if self.source_file_id is not None:
            event["source_file_id"] = self.source_file_id
        if self.policy_index is not None:
            event["policy_index"] = self.policy_index
        return event


@dataclass(frozen=True)
class SampleDataset:
    seed: int
    base_ts_ms: int
    areas: tuple[AreaSpec, ...]
    policies: tuple[dict[str, Any], ...]
    files: dict[str, SampleFile]
    events: tuple[SampleEvent, ...]
    all_base_records: bytes

    def manifest(self) -> dict[str, Any]:
        return {
            "format": SAMPLE_FORMAT,
            "seed": self.seed,
            "base_ts_ms": self.base_ts_ms,
            "record_stream_format": (
                "UTF-8 canonical JSON lines; keys sorted; separators ',' and ':'; "
                "one trailing newline per record"
            ),
            "areas": [area.to_dict() for area in self.areas],
            "policies": list(self.policies),
            "files": [self.files[file_id].to_manifest() for file_id in sorted(self.files)],
            "events": [event.to_dict() for event in self.events],
            "all_base_records_sha256": hashlib.sha256(self.all_base_records).hexdigest(),
        }


def _stable_int(seed: int, label: str, modulus: int) -> int:
    material = f"{seed}|{SAMPLE_FORMAT}|{label}".encode("utf-8")
    value = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return value % modulus


def _record_for(seed: int, area_id: str, meter_id: str, ts_ms: int) -> dict[str, Any]:
    label = f"{area_id}|{meter_id}|{ts_ms}"
    quality_roll = _stable_int(seed, label + "|quality", 100)
    if quality_roll < 84:
        quality_flag = "GOOD"
    elif quality_roll < 96:
        quality_flag = "SUSPECT"
    else:
        quality_flag = "ESTIMATED"

    return {
        "area_id": area_id,
        "meter_id": meter_id,
        "ts_ms": ts_ms,
        "active_energy_wh": 40_000 + _stable_int(seed, label + "|active", 600_000),
        "reactive_energy_varh": 5_000 + _stable_int(seed, label + "|reactive", 120_000),
        "voltage_mv": 218_000 + _stable_int(seed, label + "|voltage", 5_500),
        "current_ma": 2_000 + _stable_int(seed, label + "|current", 18_000),
        "quality_flag": quality_flag,
    }


def _area_specs() -> tuple[AreaSpec, ...]:
    specs = []
    for area_number in range(1, 4):
        area_id = f"A{area_number:02d}"
        meters = tuple(f"{area_id}-M{meter_number:02d}" for meter_number in range(1, 5))
        specs.append(
            AreaSpec(
                area_id=area_id,
                meters=meters,
                collection_interval_ms=1_000,
                batch_window_records=4,
            )
        )
    return tuple(specs)


def _records_for_window(
    seed: int,
    area: AreaSpec,
    base_ts_ms: int,
    window_index: int,
) -> list[dict[str, Any]]:
    window_start = (
        base_ts_ms
        + window_index
        * area.collection_interval_ms
        * area.batch_window_records
    )
    records = []
    for tick_index in range(area.batch_window_records):
        timestamp = window_start + tick_index * area.collection_interval_ms
        for meter_id in area.meters:
            records.append(_record_for(seed, area.area_id, meter_id, timestamp))
    return records


def _clone_with_mutation(
    source_data: bytes,
    record_index: int,
    field: str,
    new_value: Any,
) -> bytes:
    parsed = parse_record_stream(source_data)
    if record_index < 0 or record_index >= len(parsed):
        raise ValueError("mutation record index is out of range")
    records = [item.record for item in parsed]
    if field not in records[record_index]:
        raise ValueError(f"mutation field does not exist: {field}")
    if records[record_index][field] == new_value:
        raise ValueError("mutation must change the selected field")
    records[record_index][field] = new_value
    return serialize_records(records)


def generate_dataset(
    seed: int = 42,
    target_size: int = 384,
    mask_bits: int = 7,
    new_target_size: int = 768,
    new_mask_bits: int = 9,
) -> SampleDataset:
    """Build the complete deterministic sample without touching the filesystem."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")
    if seed < 0:
        raise ValueError("seed must be non-negative")
    if target_size < 16 or new_target_size < 16:
        raise ValueError("target sizes must be at least 16 bytes")
    if not (1 <= mask_bits <= 24) or not (1 <= new_mask_bits <= 24):
        raise ValueError("mask bits must be between 1 and 24")

    areas = _area_specs()
    base_streams: dict[tuple[str, int], bytes] = {}
    base_record_lists: dict[tuple[str, int], list[dict[str, Any]]] = {}
    all_base_parts: list[bytes] = []

    for area in areas:
        for window_index in range(3):
            records = _records_for_window(seed, area, BASE_TS_MS, window_index)
            data = serialize_records(records)
            base_streams[(area.area_id, window_index)] = data
            base_record_lists[(area.area_id, window_index)] = records
            all_base_parts.append(data)

    files: dict[str, SampleFile] = {}

    def add_base(file_id: str, area_id: str, window_index: int) -> None:
        data = base_streams[(area_id, window_index)]
        files[file_id] = SampleFile(
            file_id=file_id,
            area_id=area_id,
            window_index=window_index,
            data=data,
            record_count=len(base_record_lists[(area_id, window_index)]),
        )

    def add_clone(
        file_id: str,
        source_file_id: str,
        derivation: str,
        mutation: dict[str, Any] | None = None,
    ) -> None:
        source = files[source_file_id]
        if mutation is None:
            data = source.data
        else:
            data = _clone_with_mutation(
                source.data,
                mutation["record_index"],
                mutation["field"],
                mutation["new_value"],
            )
        files[file_id] = SampleFile(
            file_id=file_id,
            area_id=source.area_id,
            window_index=source.window_index,
            data=data,
            record_count=len(parse_record_stream(data)),
            derivation=derivation,
            source_file_id=source_file_id,
            mutation=mutation,
        )

    add_base("f-0001", "A01", 0)
    add_clone("f-0002", "f-0001", "exact")
    add_base("f-0003", "A02", 0)

    quality_mutation = {"record_index": 7, "field": "quality_flag", "new_value": "SUSPECT"}
    source_quality = parse_record_stream(files["f-0003"].data)[7].record["quality_flag"]
    if source_quality == "SUSPECT":
        quality_mutation["new_value"] = "GOOD"
    add_clone("f-0004", "f-0003", "near", quality_mutation)

    add_base("f-0005", "A01", 1)
    add_base("f-0006", "A03", 0)
    add_base("f-0007", "A02", 1)
    add_clone("f-0008", "f-0007", "exact")
    add_base("f-0009", "A03", 1)

    voltage_mutation = {"record_index": 5, "field": "voltage_mv", "new_value": 0}
    source_voltage = parse_record_stream(files["f-0009"].data)[5].record["voltage_mv"]
    voltage_mutation["new_value"] = source_voltage + 17
    add_clone("f-0010", "f-0009", "near", voltage_mutation)

    add_base("f-0011", "A01", 2)
    add_base("f-0012", "A02", 2)
    add_clone("f-0013", "f-0011", "exact")
    add_base("f-0014", "A03", 2)

    policies = (
        {
            "index": 0,
            "label": "initial",
            "target_size": target_size,
            "mask_bits": mask_bits,
        },
        {
            "index": 1,
            "label": "after-switch",
            "target_size": new_target_size,
            "mask_bits": new_mask_bits,
        },
    )

    event_specs = (
        ("write", "f-0001", None, None),
        ("duplicate", "f-0002", "f-0001", None),
        ("write", "f-0003", None, None),
        ("near_duplicate", "f-0004", "f-0003", None),
        ("switch_policy", None, None, 1),
        ("write", "f-0005", None, None),
        ("write", "f-0006", None, None),
        ("write", "f-0007", None, None),
        ("duplicate", "f-0008", "f-0007", None),
        ("delete", "f-0002", None, None),
        ("write", "f-0009", None, None),
        ("delete", "f-0003", None, None),
        ("near_duplicate", "f-0010", "f-0009", None),
        ("write", "f-0011", None, None),
        ("delete", "f-0001", None, None),
        ("write", "f-0012", None, None),
        ("delete", "f-0007", None, None),
        ("duplicate", "f-0013", "f-0011", None),
        ("delete", "f-0004", None, None),
        ("write", "f-0014", None, None),
        ("delete", "f-0006", None, None),
    )

    events = tuple(
        SampleEvent(
            seq=index,
            logical_time=BASE_TS_MS + index * 100,
            kind=kind,
            file_id=file_id,
            source_file_id=source_file_id,
            policy_index=policy_index,
        )
        for index, (kind, file_id, source_file_id, policy_index) in enumerate(
            event_specs, start=1
        )
    )

    return SampleDataset(
        seed=seed,
        base_ts_ms=BASE_TS_MS,
        areas=areas,
        policies=policies,
        files=files,
        events=events,
        all_base_records=b"".join(all_base_parts),
    )


def write_dataset(dataset: SampleDataset, output_dir: Path) -> dict[str, Any]:
    """Write a sample dataset using stable names and canonical JSON."""
    output_dir = Path(output_dir)
    files_dir = output_dir / "files"
    output_dir.mkdir(parents=True, exist_ok=True)
    if files_dir.exists():
        shutil.rmtree(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)

    for file_id in sorted(dataset.files):
        sample_file = dataset.files[file_id]
        (output_dir / sample_file.payload_path).write_bytes(sample_file.data)

    (output_dir / "records.jsonl").write_bytes(dataset.all_base_records)
    manifest = dataset.manifest()
    manifest_text = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ) + "\n"
    (output_dir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return manifest


def load_manifest(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if manifest.get("format") != SAMPLE_FORMAT:
        raise ValueError("unsupported sample manifest format")
    return manifest
