"""Local deterministic JSONL/Parquet sample ingestion and validation."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .errors import E_DUP_REQUEST, E_FORMAT_INVALID, fail
from .hashing import atomic_write_bytes, canonical_bytes, sha256_bytes, sha256_obj
from .models import Sample


_REQUIRED_SAMPLE_FIELDS = {
    "request_id",
    "timestamp_ms",
    "user_id",
    "item_id",
    "numeric_feats",
    "categorical_feats",
    "label_click",
    "label_convert",
}


def sample_from_dict(
    value: Any,
    *,
    line: int | None = None,
    source_name: str = "<memory>",
) -> Sample:
    if not isinstance(value, dict):
        fail(
            E_FORMAT_INVALID,
            "sample must be a JSON object",
            line=line,
            field="$",
            source=source_name,
        )
    missing = sorted(_REQUIRED_SAMPLE_FIELDS - set(value))
    if missing:
        fail(
            E_FORMAT_INVALID,
            f"required sample field {missing[0]!r} is missing",
            line=line,
            field=missing[0],
            source=source_name,
        )
    request_id = _sample_string(value["request_id"], "request_id", line)
    user_id = _sample_string(value["user_id"], "user_id", line)
    item_id = _sample_string(value["item_id"], "item_id", line)
    timestamp = value["timestamp_ms"]
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        fail(
            E_FORMAT_INVALID,
            "timestamp_ms must be an integer",
            line=line,
            field="timestamp_ms",
            source=source_name,
        )
    if timestamp < 0:
        fail(
            E_FORMAT_INVALID,
            "timestamp_ms must be non-negative",
            line=line,
            field="timestamp_ms",
            source=source_name,
        )
    numeric_raw = value["numeric_feats"]
    if not isinstance(numeric_raw, dict):
        fail(
            E_FORMAT_INVALID,
            "numeric_feats must be an object",
            line=line,
            field="numeric_feats",
            source=source_name,
        )
    numeric: dict[str, float] = {}
    for name, raw in sorted(numeric_raw.items(), key=lambda item: str(item[0])):
        if not isinstance(name, str) or not name:
            fail(
                E_FORMAT_INVALID,
                "numeric feature keys must be non-empty strings",
                line=line,
                field="numeric_feats",
                source=source_name,
            )
        if isinstance(raw, bool):
            fail(
                E_FORMAT_INVALID,
                "numeric feature value must be numeric",
                line=line,
                field=f"numeric_feats.{name}",
                source=source_name,
            )
        try:
            number = float(raw)
        except (TypeError, ValueError):
            fail(
                E_FORMAT_INVALID,
                "numeric feature value is not numeric",
                line=line,
                field=f"numeric_feats.{name}",
                source=source_name,
            )
        if not math.isfinite(number):
            fail(
                E_FORMAT_INVALID,
                "numeric feature value must be finite",
                line=line,
                field=f"numeric_feats.{name}",
                source=source_name,
            )
        numeric[name] = 0.0 if number == 0.0 else number
    categorical_raw = value["categorical_feats"]
    if not isinstance(categorical_raw, dict):
        fail(
            E_FORMAT_INVALID,
            "categorical_feats must be an object",
            line=line,
            field="categorical_feats",
            source=source_name,
        )
    categorical: dict[str, str] = {}
    for name, raw in sorted(categorical_raw.items(), key=lambda item: str(item[0])):
        if not isinstance(name, str) or not name:
            fail(
                E_FORMAT_INVALID,
                "categorical feature keys must be non-empty strings",
                line=line,
                field="categorical_feats",
                source=source_name,
            )
        if not isinstance(raw, str):
            fail(
                E_FORMAT_INVALID,
                "categorical feature value must be a string",
                line=line,
                field=f"categorical_feats.{name}",
                source=source_name,
            )
        categorical[name] = raw
    click = _binary_label(value["label_click"], "label_click", line, source_name)
    convert = _binary_label(
        value["label_convert"], "label_convert", line, source_name
    )
    return Sample(
        request_id=request_id,
        timestamp_ms=timestamp,
        user_id=user_id,
        item_id=item_id,
        numeric_feats=numeric,
        categorical_feats=categorical,
        label_click=click,
        label_convert=convert,
    )


def _sample_string(value: Any, field: str, line: int | None) -> str:
    if not isinstance(value, str) or not value.strip():
        fail(
            E_FORMAT_INVALID,
            f"{field} must be a non-empty string",
            line=line,
            field=field,
        )
    return value.strip()


def _binary_label(value: Any, field: str, line: int | None, source: str) -> int:
    if isinstance(value, bool) or value not in {0, 1}:
        fail(
            E_FORMAT_INVALID,
            f"{field} must be 0 or 1",
            line=line,
            field=field,
            source=source,
        )
    return int(value)


def load_samples_jsonl(path: str | Path) -> list[Sample]:
    source = Path(path)
    samples: list[Sample] = []
    line_numbers: dict[str, int] = {}
    with source.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                fail(
                    E_FORMAT_INVALID,
                    "invalid sample JSON",
                    line=line_number,
                    field="json",
                    column=exc.colno,
                    source=str(source),
                )
            sample = sample_from_dict(value, line=line_number, source_name=str(source))
            if sample.request_id in line_numbers:
                fail(
                    E_DUP_REQUEST,
                    f"duplicate request_id {sample.request_id!r}",
                    line=line_number,
                    field="request_id",
                    request_id=sample.request_id,
                    first_line=line_numbers[sample.request_id],
                )
            line_numbers[sample.request_id] = line_number
            samples.append(sample)
    return order_samples(samples)


def order_samples(samples: Iterable[Sample]) -> list[Sample]:
    ordered = sorted(samples, key=lambda item: (item.request_id, item.timestamp_ms))
    seen: set[str] = set()
    for item in ordered:
        if item.request_id in seen:
            fail(
                E_DUP_REQUEST,
                f"duplicate request_id {item.request_id!r}",
                field="request_id",
                request_id=item.request_id,
            )
        seen.add(item.request_id)
    return ordered


def write_samples_jsonl(path: str | Path, samples: Iterable[Sample]) -> tuple[int, str]:
    ordered = order_samples(samples)
    lines = [canonical_bytes(item.to_dict()).decode("utf-8") for item in ordered]
    data = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
    atomic_write_bytes(path, data)
    return len(ordered), sha256_bytes(data)


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - dependency error path
        raise RuntimeError(
            "pyarrow is required for columnar sample I/O; install requirements.txt"
        ) from exc
    return pa, pq


def write_samples_parquet(path: str | Path, samples: Iterable[Sample]) -> dict[str, Any]:
    """Write samples to local Parquet.

    Variable-key feature maps are stored as canonical JSON strings in two
    columns so the schema remains stable and lossless across PyArrow versions.
    """

    pa, pq = _require_pyarrow()
    ordered = order_samples(samples)
    table = pa.Table.from_pylist(
        [
            {
                "request_id": item.request_id,
                "timestamp_ms": item.timestamp_ms,
                "user_id": item.user_id,
                "item_id": item.item_id,
                "numeric_feats_json": json.dumps(
                    item.numeric_feats, sort_keys=True, separators=(",", ":")
                ),
                "categorical_feats_json": json.dumps(
                    item.categorical_feats, sort_keys=True, separators=(",", ":")
                ),
                "label_click": item.label_click,
                "label_convert": item.label_convert,
            }
            for item in ordered
        ],
        schema=pa.schema(
            [
                ("request_id", pa.string()),
                ("timestamp_ms", pa.int64()),
                ("user_id", pa.string()),
                ("item_id", pa.string()),
                ("numeric_feats_json", pa.string()),
                ("categorical_feats_json", pa.string()),
                ("label_click", pa.int8()),
                ("label_convert", pa.int8()),
            ]
        ),
    )
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, destination, compression="snappy")
    return {
        "path": str(destination),
        "rows": len(ordered),
        "schema": [field.name for field in table.schema],
        "sha256": _file_sha(destination),
    }


def load_samples_parquet(path: str | Path) -> list[Sample]:
    _, pq = _require_pyarrow()
    table = pq.read_table(Path(path))
    samples: list[Sample] = []
    for index, row in enumerate(table.to_pylist(), start=1):
        value = dict(row)
        value["numeric_feats"] = json.loads(value.pop("numeric_feats_json"))
        value["categorical_feats"] = json.loads(value.pop("categorical_feats_json"))
        samples.append(sample_from_dict(value, line=index, source_name=str(path)))
    return order_samples(samples)


def samples_checksum(samples: Iterable[Sample]) -> str:
    ordered = order_samples(samples)
    return sha256_obj([item.to_dict() for item in ordered])


def apply_label_updates(
    samples: Iterable[Sample], updates: dict[str, dict[str, int]]
) -> list[Sample]:
    unknown = sorted(set(updates) - {item.request_id for item in samples})
    if unknown:
        fail(
            E_FORMAT_INVALID,
            f"label update refers to unknown request_id {unknown[0]!r}",
            field="request_id",
            request_id=unknown[0],
        )
    result: list[Sample] = []
    for item in samples:
        update = updates.get(item.request_id, {})
        click = _binary_label(
            update.get("label_click", item.label_click),
            "label_click",
            None,
            "<label-update>",
        )
        convert = _binary_label(
            update.get("label_convert", item.label_convert),
            "label_convert",
            None,
            "<label-update>",
        )
        result.append(
            Sample(
                item.request_id,
                item.timestamp_ms,
                item.user_id,
                item.item_id,
                dict(item.numeric_feats),
                dict(item.categorical_feats),
                click,
                convert,
            )
        )
    return order_samples(result)


def _file_sha(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "sample_from_dict",
    "load_samples_jsonl",
    "load_samples_parquet",
    "write_samples_jsonl",
    "write_samples_parquet",
    "order_samples",
    "samples_checksum",
    "apply_label_updates",
]

