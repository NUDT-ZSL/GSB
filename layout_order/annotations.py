"""Readers, writers and loss-aware migrations for v1/v2/v3 annotations."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .canonical import round_float
from .errors import LayoutError

VERSION_FIELDS: dict[str, tuple[str, ...]] = {
    "v1": (
        "block_id",
        "reading_order",
        "block_type",
        "text_len",
    ),
    "v2": (
        "block_id",
        "reading_order",
        "block_type",
        "text_len",
        "confidence_low",
        "confidence_high",
        "paragraph_role",
        "order_weight",
    ),
    "v3": (
        "block_id",
        "reading_order",
        "block_type",
        "text_len",
        "confidence_low",
        "confidence_high",
        "paragraph_role",
        "order_weight",
        "column_index",
        "zone_id",
        "nesting_path",
    ),
}

_HEADER_RE = re.compile(r"^#LAYOUT_RO_(V[0-9]+)$")
_VERSION_ORDER = {"v1": 1, "v2": 2, "v3": 3}


def normalize_version(version: str) -> str:
    candidate = str(version).strip().lower()
    if candidate.startswith("v"):
        candidate = candidate[1:]
    normalized = f"v{candidate}"
    if normalized not in VERSION_FIELDS:
        raise LayoutError(
            "E_VERSION_UNSUPPORTED",
            "annotation version is not supported",
            details={"version": version},
        )
    return normalized


def default_role(block_type: str) -> str:
    return {
        "header": "header",
        "footer": "footer",
        "title": "heading",
        "document_title": "heading",
        "section_title": "heading",
        "figure_caption": "caption",
        "body": "body",
    }.get(block_type, "body")


def _default_values(block_type: str) -> dict[str, Any]:
    return {
        "confidence_low": 0.0,
        "confidence_high": 1.0,
        "paragraph_role": default_role(block_type),
        "order_weight": 1.0,
        "column_index": 0,
        "zone_id": "default",
        "nesting_path": "0",
    }


@dataclass
class AnnotationRecord:
    block_id: int
    reading_order: int
    block_type: str
    text_len: int
    confidence_low: float = 0.0
    confidence_high: float = 1.0
    paragraph_role: str = "body"
    order_weight: float = 1.0
    column_index: int = 0
    zone_id: str = "default"
    nesting_path: str = "0"

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "reading_order": self.reading_order,
            "block_type": self.block_type,
            "text_len": self.text_len,
            "confidence_low": self.confidence_low,
            "confidence_high": self.confidence_high,
            "paragraph_role": self.paragraph_role,
            "order_weight": self.order_weight,
            "column_index": self.column_index,
            "zone_id": self.zone_id,
            "nesting_path": self.nesting_path,
        }


@dataclass
class AnnotationDocument:
    version: str
    records: list[AnnotationRecord]
    implicit_fields: set[str] = field(default_factory=set)
    completions: list[dict[str, Any]] = field(default_factory=list)
    losses: list[dict[str, Any]] = field(default_factory=list)
    source: str = "<memory>"

    def sorted_records(self) -> list[AnnotationRecord]:
        return sorted(self.records, key=lambda record: (record.reading_order, record.block_id))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "records": [record.to_dict() for record in self.sorted_records()],
            "implicit_fields": sorted(self.implicit_fields),
            "completions": self.completions,
            "losses": self.losses,
            "source": self.source,
        }


@dataclass
class ArbitrationResult:
    document: AnnotationDocument
    winner_version: str
    agreement: dict[str, Any]
    provenance: dict[str, str]
    conflicts: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "winner_version": self.winner_version,
            "agreement": self.agreement,
            "provenance": self.provenance,
            "conflicts": self.conflicts,
            "document": self.document.to_dict(),
        }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_int(raw: str, version: str, field_name: str, line_no: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise LayoutError(
            "E_FORMAT_INVALID",
            "block number is not parseable as an integer",
            field=field_name,
            line=line_no,
            details={"version": version, "value": raw},
        ) from None
    if value < 0:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "block number must be non-negative",
            field=field_name,
            line=line_no,
            details={"version": version, "value": raw},
        )
    return value


def _parse_float(raw: str, version: str, field_name: str, line_no: int) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise LayoutError(
            "E_FORMAT_INVALID",
            "numeric annotation field is not parseable",
            field=field_name,
            line=line_no,
            details={"version": version, "value": raw},
        ) from None
    if not math.isfinite(value):
        raise LayoutError(
            "E_FORMAT_INVALID",
            "numeric annotation field must be finite",
            field=field_name,
            line=line_no,
            details={"version": version, "value": raw},
        )
    return value


def _detect_layout(
    lines: list[str], fmt_hint: str | None
) -> tuple[str, int, tuple[str, ...]]:
    nonempty = [
        (index + 1, line)
        for index, line in enumerate(lines)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    first_index = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "annotation file is empty",
            line=1,
        )
    first = lines[first_index].strip()
    header_match = _HEADER_RE.match(first)
    if header_match:
        raw_version = header_match.group(1).lower()
        if raw_version not in VERSION_FIELDS:
            raise LayoutError(
                "E_VERSION_UNSUPPORTED",
                "annotation header declares an unsupported version",
                line=first_index + 1,
                details={"version": raw_version},
            )
        return raw_version, first_index + 1, VERSION_FIELDS[raw_version]
    if first.startswith("#LAYOUT_RO_"):
        declared = first[len("#LAYOUT_RO_") :].strip()
        raise LayoutError(
            "E_VERSION_UNSUPPORTED",
            "annotation header declares an unsupported version",
            line=first_index + 1,
            details={"version": declared},
        )
    if fmt_hint is not None:
        version = normalize_version(fmt_hint)
        fields = tuple(first.split("|"))
        if not nonempty:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "annotation file has no data rows",
                line=first_index + 1,
            )
        return version, first_index, VERSION_FIELDS[version]
    first_fields = tuple(first.split("|"))
    for version, expected in VERSION_FIELDS.items():
        if first_fields == expected:
            return version, first_index + 1, expected
    for version, expected in VERSION_FIELDS.items():
        if len(first_fields) == len(expected):
            return version, first_index, expected
    raise LayoutError(
        "E_FORMAT_INVALID",
        "annotation field set does not match any supported version",
        line=first_index + 1,
        details={"field_count": len(first_fields)},
    )


def parse_annotations(
    text: str, *, fmt_hint: str | None = None, source: str = "<memory>"
) -> AnnotationDocument:
    """Parse header-delimited pipe-separated annotations.

    v1: 4 fields, v2: 8 fields, v3: 11 fields. A version header is preferred;
    when absent, the field set is used to select the parsing path.
    """

    lines = text.splitlines()
    version, data_start, fields = _detect_layout(lines, fmt_hint)
    order_index = fields.index("reading_order")
    records: list[AnnotationRecord] = []
    seen_ids: set[int] = set()
    seen_orders: dict[int, int] = {}
    for index in range(data_start, len(lines)):
        raw_line = lines[index]
        if not raw_line.strip():
            continue
        line_no = index + 1
        values = raw_line.split("|")
        if len(values) != len(fields):
            raise LayoutError(
                "E_FORMAT_INVALID",
                "annotation row field count does not match version",
                line=line_no,
                field="field_count",
                details={
                    "version": version,
                    "expected": len(fields),
                    "actual": len(values),
                },
            )
        row = dict(zip(fields, values))
        for field_name, raw in row.items():
            if raw == "":
                raise LayoutError(
                    "E_FORMAT_INVALID",
                    "required annotation field is empty",
                    line=line_no,
                    field=field_name,
                    details={"version": version},
                )
        block_id = _parse_int(row["block_id"], version, "block_id", line_no)
        reading_order = _parse_int(
            row["reading_order"], version, "reading_order", line_no
        )
        text_len = _parse_int(row["text_len"], version, "text_len", line_no)
        if block_id in seen_ids:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "duplicate block_id",
                line=line_no,
                field="block_id",
                block_id=block_id,
            )
        if reading_order in seen_orders:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "duplicate reading_order value",
                line=line_no,
                field="reading_order",
                block_id=block_id,
                details={"first_line": seen_orders[reading_order]},
            )
        seen_ids.add(block_id)
        seen_orders[reading_order] = line_no
        defaults = _default_values(row["block_type"])
        if version == "v1":
            confidence_low = defaults["confidence_low"]
            confidence_high = defaults["confidence_high"]
            paragraph_role = defaults["paragraph_role"]
            order_weight = defaults["order_weight"]
            column_index = defaults["column_index"]
            zone_id = defaults["zone_id"]
            nesting_path = defaults["nesting_path"]
        elif version == "v2":
            confidence_low = _parse_float(
                row["confidence_low"], version, "confidence_low", line_no
            )
            confidence_high = _parse_float(
                row["confidence_high"], version, "confidence_high", line_no
            )
            paragraph_role = row["paragraph_role"]
            order_weight = _parse_float(
                row["order_weight"], version, "order_weight", line_no
            )
            column_index = defaults["column_index"]
            zone_id = defaults["zone_id"]
            nesting_path = defaults["nesting_path"]
        else:
            confidence_low = _parse_float(
                row["confidence_low"], version, "confidence_low", line_no
            )
            confidence_high = _parse_float(
                row["confidence_high"], version, "confidence_high", line_no
            )
            paragraph_role = row["paragraph_role"]
            order_weight = _parse_float(
                row["order_weight"], version, "order_weight", line_no
            )
            column_index = _parse_int(
                row["column_index"], version, "column_index", line_no
            )
            zone_id = row["zone_id"]
            nesting_path = row["nesting_path"]
        if confidence_low > confidence_high:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "confidence interval lower bound exceeds upper bound",
                line=line_no,
                field="confidence_low",
                block_id=block_id,
            )
        if order_weight < 0:
            raise LayoutError(
                "E_FORMAT_INVALID",
                "order_weight must be non-negative",
                line=line_no,
                field="order_weight",
                block_id=block_id,
            )
        records.append(
            AnnotationRecord(
                block_id=block_id,
                reading_order=reading_order,
                block_type=row["block_type"],
                text_len=text_len,
                confidence_low=round_float(confidence_low, 8),
                confidence_high=round_float(confidence_high, 8),
                paragraph_role=paragraph_role,
                order_weight=round_float(order_weight, 8),
                column_index=column_index,
                zone_id=zone_id,
                nesting_path=nesting_path,
            )
        )
    if not records:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "annotation file has no data rows",
            line=data_start + 1 if data_start < len(lines) else len(lines),
        )
    ordered_orders = sorted(record.reading_order for record in records)
    if ordered_orders != list(range(ordered_orders[0], ordered_orders[0] + len(records))):
        # Non-contiguous order values are still unambiguous after sorting, but
        # they cannot form a canonical reading sequence without a declared gap
        # rule, so reject them instead of silently renumbering.
        expected = set(range(ordered_orders[0], ordered_orders[0] + len(records)))
        missing = sorted(expected - set(ordered_orders))
        if missing:
            first_line = min(
                (
                    index + 1
                    for index in range(data_start, len(lines))
                    if lines[index].strip()
                ),
                default=data_start + 1,
            )
            raise LayoutError(
                "E_FORMAT_INVALID",
                "reading_order values must be contiguous",
                line=first_line,
                field="reading_order",
                details={"missing": missing},
            )
    implicit = set()
    if version == "v1":
        implicit.update(
            {
                "confidence_low",
                "confidence_high",
                "paragraph_role",
                "order_weight",
                "column_index",
                "zone_id",
                "nesting_path",
            }
        )
    elif version == "v2":
        implicit.update({"column_index", "zone_id", "nesting_path"})
    return AnnotationDocument(
        version=version,
        records=records,
        implicit_fields=implicit,
        source=source,
    )


def load_annotations(
    source: str | Path, *, fmt_hint: str | None = None
) -> AnnotationDocument:
    path = Path(source)
    try:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            label = str(path)
        else:
            text = str(source)
            label = "<memory>"
    except OSError as exc:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "annotation source cannot be read",
            details={"source": str(source), "error": str(exc)},
        ) from exc
    return parse_annotations(text, fmt_hint=fmt_hint, source=label)


def _format_number(value: float) -> str:
    return f"{round_float(float(value), 8):.8f}".rstrip("0").rstrip(".") or "0"


def _validate_field_text(value: str, field_name: str, block_id: int) -> str:
    if "|" in value or "\n" in value or "\r" in value:
        raise LayoutError(
            "E_FORMAT_INVALID",
            "annotation field contains a reserved delimiter",
            field=field_name,
            block_id=block_id,
        )
    return value


def render_annotations(document: AnnotationDocument, version: str) -> str:
    version = normalize_version(version)
    fields = VERSION_FIELDS[version]
    lines = [f"#LAYOUT_RO_{version.upper()}"]
    for record in document.sorted_records():
        data = record.to_dict()
        row: list[str] = []
        for field_name in fields:
            value = data[field_name]
            if isinstance(value, float):
                row.append(_format_number(value))
            else:
                row.append(_validate_field_text(str(value), field_name, record.block_id))
        lines.append("|".join(row))
    return "\n".join(lines) + "\n"


def _target_default(record: AnnotationRecord, field_name: str) -> Any:
    if field_name == "paragraph_role":
        return default_role(record.block_type)
    return _default_values(record.block_type)[field_name]


def _losses_for_downgrade(
    document: AnnotationDocument, target_version: str
) -> list[dict[str, Any]]:
    if target_version == "v3":
        return []
    unavailable: tuple[str, ...]
    if target_version == "v2":
        unavailable = ("column_index", "zone_id", "nesting_path")
    elif target_version == "v1":
        unavailable = (
            "confidence_low",
            "confidence_high",
            "paragraph_role",
            "order_weight",
            "column_index",
            "zone_id",
            "nesting_path",
        )
    else:
        unavailable = ()
    losses: list[dict[str, Any]] = []
    for record in document.sorted_records():
        data = record.to_dict()
        for field_name in unavailable:
            default = _target_default(record, field_name)
            value = data[field_name]
            same = (
                abs(float(value) - float(default)) <= 1e-12
                if isinstance(default, float)
                else value == default
            )
            if same:
                continue
            if field_name in {"column_index", "zone_id", "nesting_path"}:
                rule = "flatten_multi_column_nesting"
            elif field_name == "order_weight":
                rule = "drop_order_weight"
            else:
                rule = "drop_unrepresentable_metadata"
            losses.append(
                {
                    "source_block_id": record.block_id,
                    "field": field_name,
                    "original": value,
                    "replacement": default,
                    "rule": rule,
                    "target_version": target_version,
                }
            )
    return losses


def migrate_annotations(
    document: AnnotationDocument,
    target_version: str,
    *,
    allow_loss: bool = False,
) -> AnnotationDocument:
    target = normalize_version(target_version)
    source = normalize_version(document.version)
    if _VERSION_ORDER[target] < _VERSION_ORDER[source]:
        losses = _losses_for_downgrade(document, target)
        if losses and not allow_loss:
            first = min(item["source_block_id"] for item in losses)
            raise LayoutError(
                "E_SEMANTIC_DRIFT",
                "downgrade cannot preserve all annotation semantics",
                block_id=first,
                details={"target_version": target, "losses": losses},
            )
    else:
        losses = []
    source_fields = set(VERSION_FIELDS[source])
    target_fields = set(VERSION_FIELDS[target])
    newly_available = target_fields - source_fields
    completions = [dict(item) for item in document.completions]
    records: list[AnnotationRecord] = []
    for record in document.sorted_records():
        data = record.to_dict()
        for field_name in sorted(newly_available):
            if _VERSION_ORDER[target] > _VERSION_ORDER[source]:
                default = _target_default(record, field_name)
                data[field_name] = default
                if field_name in {"column_index", "zone_id", "nesting_path"}:
                    rule = "column_flatten_default"
                elif field_name == "paragraph_role":
                    rule = "block_type_role_mapping"
                elif field_name == "order_weight":
                    rule = "unit_weight_default"
                else:
                    rule = "confidence_interval_default"
                completions.append(
                    {
                        "source_block_id": record.block_id,
                        "field": field_name,
                        "value": default,
                        "source": f"{source}_default",
                        "rule": rule,
                    }
                )
        # Downgrading materializes the target's implicit defaults.
        for field_name in target_fields - source_fields:
            if field_name not in data:
                data[field_name] = _target_default(record, field_name)
        for field_name in source_fields - target_fields:
            data.pop(field_name, None)
        defaults = _target_default(record, "column_index")
        records.append(
            AnnotationRecord(
                block_id=data["block_id"],
                reading_order=data["reading_order"],
                block_type=data["block_type"],
                text_len=data["text_len"],
                confidence_low=float(data.get("confidence_low", 0.0)),
                confidence_high=float(data.get("confidence_high", 1.0)),
                paragraph_role=str(data.get("paragraph_role", default_role(data["block_type"]))),
                order_weight=float(data.get("order_weight", 1.0)),
                column_index=int(data.get("column_index", defaults)),
                zone_id=str(data.get("zone_id", "default")),
                nesting_path=str(data.get("nesting_path", "0")),
            )
        )
    implicit = set()
    if target == "v3":
        implicit = set()
    elif target == "v2":
        implicit = {"column_index", "zone_id", "nesting_path"}
    elif target == "v1":
        implicit = {
            "confidence_low",
            "confidence_high",
            "paragraph_role",
            "order_weight",
            "column_index",
            "zone_id",
            "nesting_path",
        }
    return AnnotationDocument(
        version=target,
        records=records,
        implicit_fields=implicit,
        completions=completions,
        losses=losses,
        source=document.source,
    )


def write_annotations(
    document: AnnotationDocument,
    version: str,
    path: str | Path | None = None,
    *,
    allow_loss: bool = False,
) -> str:
    target = normalize_version(version)
    migrated = migrate_annotations(document, target, allow_loss=allow_loss)
    text = render_annotations(migrated, target)
    if path is not None:
        target_path = Path(path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(text, encoding="utf-8")
    return text


def semantic_signature(
    document: AnnotationDocument, *, include_metadata: bool = False
) -> dict[str, Any]:
    ordered = document.sorted_records()
    signature: dict[str, Any] = {
        "block_ids": sorted(record.block_id for record in ordered),
        "sequence": [record.block_id for record in ordered],
        "order_weights": {
            str(record.block_id): round_float(record.order_weight, 8)
            for record in sorted(ordered, key=lambda item: item.block_id)
        },
    }
    if include_metadata:
        signature["metadata"] = {
            str(record.block_id): {
                "confidence_low": record.confidence_low,
                "confidence_high": record.confidence_high,
                "paragraph_role": record.paragraph_role,
                "column_index": record.column_index,
                "zone_id": record.zone_id,
                "nesting_path": record.nesting_path,
            }
            for record in sorted(ordered, key=lambda item: item.block_id)
        }
    return signature


def _first_semantic_difference(
    reference: AnnotationDocument, other: AnnotationDocument
) -> tuple[int, str, Any, Any] | None:
    ref_sequence = [record.block_id for record in reference.sorted_records()]
    other_sequence = [record.block_id for record in other.sorted_records()]
    if set(ref_sequence) != set(other_sequence):
        missing = sorted(set(ref_sequence) - set(other_sequence)) or sorted(
            set(other_sequence) - set(ref_sequence)
        )
        return (missing[0], "block_set", ref_sequence, other_sequence)
    ref_position = {block_id: index for index, block_id in enumerate(ref_sequence)}
    other_position = {block_id: index for index, block_id in enumerate(other_sequence)}
    for block_id in ref_sequence:
        if ref_position[block_id] != other_position[block_id]:
            return (block_id, "sequence", ref_sequence, other_sequence)
    ref_weights = {
        record.block_id: round_float(record.order_weight, 8)
        for record in reference.records
    }
    other_weights = {
        record.block_id: round_float(record.order_weight, 8) for record in other.records
    }
    for block_id in sorted(ref_weights):
        if abs(ref_weights[block_id] - other_weights[block_id]) > 1e-8:
            return (
                block_id,
                "order_weight",
                ref_weights[block_id],
                other_weights[block_id],
            )
    return None


def arbitrate_annotations(
    documents: Iterable[AnnotationDocument],
    *,
    require_metadata_agreement: bool = False,
) -> ArbitrationResult:
    docs = list(documents)
    if not docs:
        raise LayoutError(
            "E_SEMANTIC_DRIFT",
            "at least one annotation document is required",
        )
    reference = max(docs, key=lambda doc: _VERSION_ORDER[normalize_version(doc.version)])
    conflicts: list[dict[str, Any]] = []
    for document in docs:
        difference = _first_semantic_difference(reference, document)
        if difference is not None:
            block_id, field_name, left, right = difference
            raise LayoutError(
                "E_SEMANTIC_DRIFT",
                "annotation documents disagree",
                block_id=block_id,
                details={
                    "field": field_name,
                    "reference_version": reference.version,
                    "other_version": document.version,
                    "reference": left,
                    "other": right,
                },
            )
        if require_metadata_agreement and reference.version == "v3":
            ref_map = {
                record.block_id: {
                    "confidence_low": record.confidence_low,
                    "confidence_high": record.confidence_high,
                    "paragraph_role": record.paragraph_role,
                    "column_index": record.column_index,
                    "zone_id": record.zone_id,
                    "nesting_path": record.nesting_path,
                }
                for record in reference.records
            }
            other_map = {
                record.block_id: {
                    "confidence_low": record.confidence_low,
                    "confidence_high": record.confidence_high,
                    "paragraph_role": record.paragraph_role,
                    "column_index": record.column_index,
                    "zone_id": record.zone_id,
                    "nesting_path": record.nesting_path,
                }
                for record in document.records
            }
            for block_id in sorted(ref_map):
                for field_name in sorted(ref_map[block_id]):
                    if ref_map[block_id][field_name] != other_map[block_id][field_name]:
                        conflicts.append(
                            {
                                "source_block_id": block_id,
                                "field": field_name,
                                "winner": ref_map[block_id][field_name],
                                "loser": other_map[block_id][field_name],
                                "rule": "highest_version_wins",
                            }
                        )
    provenance: dict[str, str] = {}
    all_fields = set().union(*(VERSION_FIELDS[doc.version] for doc in docs))
    for field_name in sorted(all_fields):
        candidates = [
            doc
            for doc in docs
            if field_name in VERSION_FIELDS[doc.version]
            and field_name not in doc.implicit_fields
        ]
        if candidates:
            winner = max(
                candidates, key=lambda item: _VERSION_ORDER[normalize_version(item.version)]
            )
            provenance[field_name] = winner.version
        else:
            provenance[field_name] = "implicit_default"
    canonical = migrate_annotations(reference, "v3", allow_loss=True)
    agreement = semantic_signature(reference)
    return ArbitrationResult(
        document=canonical,
        winner_version=reference.version,
        agreement=agreement,
        provenance=provenance,
        conflicts=conflicts,
    )

