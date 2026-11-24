from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .masking import (
    DEFAULT_MASK_METHODS,
    canonical_field_path,
    canonical_mask_method,
    mask_method_allowed,
)
from .models import (
    CategoryDefault,
    Event,
    LayoutPolicy,
    Rejection,
    RuleRecord,
)
from .util import (
    bounded_raw,
    canonical_digest,
    canonical_json,
    is_nonnegative_int,
    parse_int_text,
)


RULE_ALIASES = {
    "rule_name": (
        "rule_name",
        "name",
        "rule",
        "canonical_name",
        "canonical_rule_name",
        "rule_id",
    ),
    "field_path": ("field_path", "field", "path", "payload_path"),
    "mask": ("mask", "mask_method", "masking", "mask_type"),
    "threshold": ("threshold", "priority", "rank"),
    "effective_ms": (
        "effective_ms",
        "effective_at_ms",
        "effective_at",
        "effective",
        "effective_time_ms",
        "absolute_effective_ms",
    ),
    "generation": ("generation", "rule_generation", "rule_version", "gen"),
    "tombstone": ("tombstone", "is_tombstone", "deleted", "retired"),
}


@dataclass
class ParsedSnapshot:
    source_ref: str
    records: List[RuleRecord] = field(default_factory=list)
    defaults: Dict[str, CategoryDefault] = field(default_factory=dict)
    layout: Optional[LayoutPolicy] = None
    observation_clock_ms: Optional[int] = None
    snapshot_id: Optional[str] = None
    explicit_empty: bool = False
    rejections: List[Rejection] = field(default_factory=list)
    fatal_errors: List[Rejection] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.fatal_errors and self.layout is not None


@dataclass
class ParsedPatch:
    patch_id: str
    source_ref: str
    changed_rules: List[str]
    tombstones: List[str]
    records: List[RuleRecord]
    rejections: List[Rejection]
    raw: str

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "patch_id": self.patch_id,
                "changed_rules": self.changed_rules,
                "tombstones": self.tombstones,
                "records": [
                    {
                        "rule_name": record.rule_name,
                        "field_path": record.field_path,
                        "mask": record.mask,
                        "threshold": record.threshold,
                        "effective_ms": record.effective_ms,
                        "generation": record.generation,
                        "tombstone": record.tombstone,
                    }
                    for record in self.records
                ],
            }
        )


@dataclass
class ParsedPatchFile:
    source_ref: str
    patches: List[ParsedPatch] = field(default_factory=list)
    rejections: List[Rejection] = field(default_factory=list)
    fatal_errors: List[Rejection] = field(default_factory=list)


def _rejection(
    source_format: str,
    source_ref: str,
    index: int,
    code: str,
    message: str,
    raw: Any = "",
) -> Rejection:
    raw_text = raw if isinstance(raw, str) else canonical_json(raw)
    return Rejection(
        source_format=source_format,
        source_ref=source_ref,
        record_index=index,
        code=code,
        message=message,
        raw=bounded_raw(raw_text),
    )


def _single_alias(
    mapping: Dict[str, Any],
    aliases: Sequence[str],
) -> Tuple[Optional[str], Any, Optional[str]]:
    present = [(key, mapping[key]) for key in aliases if key in mapping]
    if not present:
        return None, None, None
    first_key, first_value = present[0]
    for key, value in present[1:]:
        if canonical_json(value) != canonical_json(first_value):
            return (
                None,
                None,
                f"conflicting aliases {first_key!r} and {key!r} are both present",
            )
    return first_key, first_value, None


def _parse_json_document(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _coerce_json_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _coerce_text_int(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return parse_int_text(value)
    return None


def _coerce_text_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes"):
            return True
        if lowered in ("false", "0", "no"):
            return False
    return None


def parse_events_lines(
    lines: Iterable[str],
    source_ref: str,
    *,
    seen_event_keys: Optional[set] = None,
    seen_event_ids: Optional[set] = None,
) -> Tuple[List[Event], List[Rejection]]:
    events: List[Event] = []
    rejections: List[Rejection] = []
    seen = seen_event_keys if seen_event_keys is not None else set()
    seen_ids = (
        seen_event_ids
        if seen_event_ids is not None
        else {item[0] for item in seen if isinstance(item, tuple) and len(item) == 2}
    )

    for index, line in enumerate(lines, start=1):
        if not line.strip():
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_PARSE",
                    "blank line is not a JSON event record",
                    line,
                )
            )
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_PARSE",
                    f"invalid JSON: {exc.msg}",
                    line,
                )
            )
            continue
        if not isinstance(raw, dict):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_PARSE",
                    "event record must be a JSON object",
                    line,
                )
            )
            continue

        event_id = raw.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_EVENT_ID",
                    "event_id must be a non-empty string",
                    line,
                )
            )
            continue
        seq = raw.get("seq")
        if not is_nonnegative_int(seq):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_SEQ",
                    "seq must be a non-negative integer",
                    line,
                )
            )
            continue
        key = (event_id, seq)
        if key in seen:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_DUPLICATE_EVENT",
                    f"duplicate event key (event_id={event_id!r}, seq={seq})",
                    line,
                )
            )
            continue
        if event_id in seen_ids:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_DUPLICATE_EVENT_ID",
                    f"event_id {event_id!r} is not unique in the event stream",
                    line,
                )
            )
            continue

        tenant = raw.get("tenant")
        actor = raw.get("actor")
        category = raw.get("category")
        if not isinstance(tenant, str) or not tenant:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_TENANT",
                    "tenant must be a non-empty string",
                    line,
                )
            )
            continue
        if not isinstance(actor, str):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_ACTOR",
                    "actor must be a string",
                    line,
                )
            )
            continue
        if not isinstance(category, str) or not category:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_CATEGORY",
                    "category must be a non-empty string",
                    line,
                )
            )
            continue
        ts_ms = raw.get("ts_ms")
        if not is_nonnegative_int(ts_ms):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_TS_MS",
                    "ts_ms must be a non-negative integer",
                    line,
                )
            )
            continue
        payload = raw.get("payload")
        if not isinstance(payload, dict):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_PAYLOAD",
                    "payload must be an object",
                    line,
                )
            )
            continue
        bad_payload = next(
            (
                key
                for key, value in payload.items()
                if not isinstance(key, str) or not isinstance(value, str)
            ),
            None,
        )
        if bad_payload is not None:
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_PAYLOAD",
                    f"payload key {bad_payload!r} must map to a string",
                    line,
                )
            )
            continue
        idempotency_key = raw.get("idempotency_key")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_IDEMPOTENCY_KEY",
                    "idempotency_key must be a non-empty string",
                    line,
                )
            )
            continue
        sync_marker = raw.get("sync_marker")
        if sync_marker is not None and (
            not isinstance(sync_marker, str) or not sync_marker.strip()
        ):
            rejections.append(
                _rejection(
                    "events-jsonl",
                    source_ref,
                    index,
                    "E_SYNC_MARKER",
                    "sync_marker, when present, must be a non-empty string",
                    line,
                )
            )
            continue

        seen.add(key)
        seen_ids.add(event_id)
        events.append(
            Event(
                event_id=event_id,
                tenant=tenant,
                actor=actor,
                category=category,
                ts_ms=ts_ms,
                seq=seq,
                payload=dict(sorted(payload.items())),
                idempotency_key=idempotency_key,
                sync_marker=sync_marker,
                source_ref=source_ref,
                source_index=index,
            )
        )
    return events, rejections


def parse_events_file(
    path: str | Path,
    *,
    seen_event_keys: Optional[set] = None,
    seen_event_ids: Optional[set] = None,
) -> Tuple[List[Event], List[Rejection]]:
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return parse_events_lines(
                handle,
                str(file_path),
                seen_event_keys=seen_event_keys,
                seen_event_ids=seen_event_ids,
            )
    except OSError as exc:
        return [], [
            _rejection(
                "events-jsonl",
                str(file_path),
                0,
                "E_IO",
                f"cannot read event file: {exc}",
            )
        ]


def _parse_layout(
    value: Any,
    source_ref: str,
    index: int,
) -> Tuple[Optional[LayoutPolicy], List[Rejection]]:
    rejections: List[Rejection] = []
    if value is None:
        return LayoutPolicy(DEFAULT_MASK_METHODS), rejections
    if not isinstance(value, dict):
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_LAYOUT",
                "layout must be an object",
                value,
            )
        )
        return None, rejections

    raw_methods = value.get("mask_methods", list(DEFAULT_MASK_METHODS))
    if not isinstance(raw_methods, list) or not raw_methods:
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_LAYOUT",
                "layout.mask_methods must be a non-empty array",
                value,
            )
        )
        return None, rejections
    methods: List[str] = []
    for item in raw_methods:
        if not isinstance(item, str) or not item.strip():
            rejections.append(
                _rejection(
                    "rules-metadata",
                    source_ref,
                    index,
                    "E_LAYOUT",
                    "layout.mask_methods entries must be non-empty strings",
                    value,
                )
            )
            return None, rejections
        method = canonical_mask_method(item)
        if method not in DEFAULT_MASK_METHODS:
            rejections.append(
                _rejection(
                    "rules-metadata",
                    source_ref,
                    index,
                    "E_LAYOUT",
                    f"layout contains unsupported mask method {item!r}",
                    value,
                )
            )
            return None, rejections
        methods.append(method)
    if len(methods) != len(set(methods)):
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_LAYOUT",
                "layout.mask_methods must not contain duplicates",
                value,
            )
        )
        return None, rejections

    threshold_mode = value.get("threshold_mode", "higher_wins")
    if threshold_mode != "higher_wins":
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_LAYOUT",
                "only threshold_mode='higher_wins' is supported",
                value,
            )
        )
        return None, rejections
    version = value.get("version", "audit-mask-layout-v1")
    if not isinstance(version, str) or not version.strip():
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_LAYOUT",
                "layout.version must be a non-empty string",
                value,
            )
        )
        return None, rejections
    return (
        LayoutPolicy(
            mask_methods=tuple(methods),
            threshold_mode=threshold_mode,
            version=version,
        ),
        rejections,
    )


def _parse_category_defaults(
    value: Any,
    layout: LayoutPolicy,
    source_ref: str,
    index: int,
) -> Tuple[Dict[str, CategoryDefault], List[Rejection]]:
    defaults: Dict[str, CategoryDefault] = {}
    rejections: List[Rejection] = []
    if value is None:
        return defaults, rejections
    if not isinstance(value, dict):
        rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                index,
                "E_CATEGORY_DEFAULT",
                "category_defaults must be an object",
                value,
            )
        )
        return defaults, rejections
    for category, spec in value.items():
        if not isinstance(category, str) or not category.strip():
            rejections.append(
                _rejection(
                    "category-default",
                    source_ref,
                    index,
                    "E_CATEGORY_DEFAULT",
                    "category names must be non-empty strings",
                    spec,
                )
            )
            continue
        if isinstance(spec, str):
            mask = canonical_mask_method(spec)
            fields = ["payload.*"]
            rule_name = f"default:{category}"
            generation = 0
        elif isinstance(spec, dict):
            raw_mask = spec.get("mask", spec.get("mask_method"))
            if not isinstance(raw_mask, str):
                rejections.append(
                    _rejection(
                        "category-default",
                        source_ref,
                        index,
                        "E_CATEGORY_DEFAULT",
                        f"default for category {category!r} requires a mask",
                        spec,
                    )
                )
                continue
            mask = canonical_mask_method(raw_mask)
            raw_fields = spec.get(
                "fields",
                spec.get("field_paths", spec.get("paths", ["payload.*"])),
            )
            if isinstance(raw_fields, str):
                raw_fields = [raw_fields]
            if not isinstance(raw_fields, list) or not raw_fields:
                rejections.append(
                    _rejection(
                        "category-default",
                        source_ref,
                        index,
                        "E_CATEGORY_DEFAULT",
                        f"default for category {category!r} has invalid fields",
                        spec,
                    )
                )
                continue
            fields = []
            for field_path in raw_fields:
                if not isinstance(field_path, str) or not field_path.strip():
                    rejections.append(
                        _rejection(
                            "category-default",
                            source_ref,
                            index,
                            "E_CATEGORY_DEFAULT",
                            f"default for category {category!r} has invalid field path",
                            spec,
                        )
                    )
                    fields = []
                    break
                fields.append(canonical_field_path(field_path))
            if not fields:
                continue
            rule_name = spec.get("rule_name", f"default:{category}")
            if not isinstance(rule_name, str) or not rule_name.strip():
                rejections.append(
                    _rejection(
                        "category-default",
                        source_ref,
                        index,
                        "E_CATEGORY_DEFAULT",
                        f"default for category {category!r} has invalid rule_name",
                        spec,
                    )
                )
                continue
            generation = spec.get("generation", 0)
            if not is_nonnegative_int(generation):
                rejections.append(
                    _rejection(
                        "category-default",
                        source_ref,
                        index,
                        "E_CATEGORY_DEFAULT",
                        f"default for category {category!r} has invalid generation",
                        spec,
                    )
                )
                continue
        else:
            rejections.append(
                _rejection(
                    "category-default",
                    source_ref,
                    index,
                    "E_CATEGORY_DEFAULT",
                    f"default for category {category!r} must be an object or mask string",
                    spec,
                )
            )
            continue
        if not mask_method_allowed(mask, layout.mask_methods):
            rejections.append(
                _rejection(
                    "category-default",
                    source_ref,
                    index,
                    "E_MASK_NOT_ALLOWED",
                    f"default mask {mask!r} is not in the layout whitelist",
                    spec,
                )
            )
            continue
        defaults[category] = CategoryDefault(
            category=category,
            fields=tuple(sorted(set(fields))),
            mask=mask,
            rule_name=rule_name,
            generation=generation,
        )
    return defaults, rejections


def _v1_relative_seconds(
    entry: Any,
    external_relative: Any,
) -> Tuple[Optional[int], Optional[str]]:
    if isinstance(entry, dict):
        for key in (
            "relative_effective_seconds",
            "relative_effective_sec",
            "relative_sec",
        ):
            if key in entry:
                parsed = _coerce_text_int(entry[key])
                if parsed is None or parsed < 0:
                    return None, f"{key} must be a non-negative integer"
                return parsed, None
    if external_relative is None:
        return 0, None
    parsed = _coerce_text_int(external_relative)
    if parsed is None or parsed < 0:
        return None, "v1 relative effective seconds must be a non-negative integer"
    return parsed, None


def _parse_v1_record(
    line: str,
    *,
    source_ref: str,
    record_index: int,
    observation_clock_ms: Optional[int],
    relative_seconds: int,
    generation: int,
    allowed_masks: Sequence[str],
) -> Tuple[Optional[RuleRecord], Optional[Rejection]]:
    parts = [part.strip() for part in line.split("|")]
    if len(parts) != 4:
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_V1_FIELDS",
            "v1 rule line must contain exactly four pipe-separated fields",
            line,
        )
    rule_name, field_path, mask_raw, threshold_raw = parts
    if not rule_name:
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_RULE_NAME",
            "v1 rule name must be non-empty",
            line,
        )
    if not field_path:
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_FIELD_PATH",
            "v1 field path must be non-empty",
            line,
        )
    if observation_clock_ms is None:
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_RULE_CLOCK",
            "v1 rules require the snapshot observation clock",
            line,
        )
    threshold = parse_int_text(threshold_raw)
    if threshold is None or threshold < 0:
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_THRESHOLD",
            "v1 threshold must be a non-negative integer",
            line,
        )
    mask = canonical_mask_method(mask_raw)
    if not mask_method_allowed(mask, allowed_masks):
        return None, _rejection(
            "v1-pipe-line",
            source_ref,
            record_index,
            "E_MASK_NOT_ALLOWED",
            f"v1 mask {mask_raw!r} is not in the layout whitelist",
            line,
        )
    return (
        RuleRecord(
            source_kind="snapshot",
            source_ref=source_ref,
            record_index=record_index,
            source_format="v1-pipe-line",
            raw=line,
            rule_name=rule_name,
            field_path=canonical_field_path(field_path),
            mask=mask,
            threshold=threshold,
            effective_ms=observation_clock_ms + relative_seconds * 1000,
            generation=generation,
            tombstone=False,
        ),
        None,
    )


def _parse_v2_key_value_text(
    text: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return None, f"invalid JSON object: {exc.msg}"
        if not isinstance(parsed, dict):
            return None, "v2 JSON record must be an object"
        return parsed, None
    pairs: Dict[str, Any] = {}
    tokens = []
    for separator in (";", "|"):
        if separator in stripped:
            tokens = [token.strip() for token in stripped.split(separator)]
            break
    if not tokens:
        tokens = [stripped]
    for token in tokens:
        if not token:
            return None, "empty key-value token"
        if "=" not in token:
            return None, f"v2 token {token!r} is not a key=value pair"
        key, value = token.split("=", 1)
        key = key.strip()
        if not key:
            return None, "empty v2 key"
        if key in pairs and str(pairs[key]) != value.strip():
            return None, f"duplicate v2 key {key!r}"
        pairs[key] = value.strip()
    return pairs, None


def _parse_v2_record(
    mapping: Any,
    *,
    source_kind: str,
    source_ref: str,
    record_index: int,
    patch_id: Optional[str],
    allowed_masks: Sequence[str],
    raw_override: Optional[str] = None,
) -> Tuple[Optional[RuleRecord], Optional[Rejection]]:
    source_format = (
        "v2-key-value"
        if raw_override is not None or not isinstance(mapping, dict)
        else "v2-json-object"
    )
    raw_text = raw_override if raw_override is not None else canonical_json(mapping)
    if not isinstance(mapping, dict):
        return None, _rejection(
            source_format,
            source_ref,
            record_index,
            "E_V2_FIELDS",
            "v2 record must be a JSON object or key=value line",
            raw_text,
        )

    selected: Dict[str, Any] = {}
    for canonical_name, aliases in RULE_ALIASES.items():
        _, value, error = _single_alias(mapping, aliases)
        if error:
            return None, _rejection(
                source_format,
                source_ref,
                record_index,
                "E_V2_AMBIGUOUS",
                error,
                raw_text,
            )
        selected[canonical_name] = value

    rule_name = selected["rule_name"]
    if not isinstance(rule_name, str) or not rule_name.strip():
        return None, _rejection(
            source_format,
            source_ref,
            record_index,
            "E_RULE_NAME",
            "v2 rule_name is required and must be a non-empty string",
            raw_text,
        )
    effective_ms = _coerce_text_int(selected["effective_ms"])
    if effective_ms is None or effective_ms < 0:
        return None, _rejection(
            source_format,
            source_ref,
            record_index,
            "E_EFFECTIVE_MS",
            "v2 effective_ms is required and must be a non-negative integer",
            raw_text,
        )
    generation = _coerce_text_int(selected["generation"])
    if generation is None or generation < 0:
        return None, _rejection(
            source_format,
            source_ref,
            record_index,
            "E_GENERATION",
            "v2 generation is required and must be a non-negative integer",
            raw_text,
        )
    tombstone = _coerce_text_bool(selected["tombstone"])
    if tombstone is None:
        return None, _rejection(
            source_format,
            source_ref,
            record_index,
            "E_TOMBSTONE",
            "v2 tombstone is required and must be a boolean",
            raw_text,
        )

    field_path_raw = selected["field_path"]
    mask_raw = selected["mask"]
    threshold_raw = selected["threshold"]
    if tombstone:
        field_path = (
            canonical_field_path(field_path_raw)
            if isinstance(field_path_raw, str) and field_path_raw.strip()
            else None
        )
        mask = (
            canonical_mask_method(mask_raw)
            if isinstance(mask_raw, str) and mask_raw.strip()
            else None
        )
        threshold = (
            _coerce_text_int(threshold_raw)
            if threshold_raw is not None
            else None
        )
        if threshold is not None and threshold < 0:
            threshold = None
    else:
        if not isinstance(field_path_raw, str) or not field_path_raw.strip():
            return None, _rejection(
                source_format,
                source_ref,
                record_index,
                "E_FIELD_PATH",
                "non-tombstone v2 rule requires field_path",
                raw_text,
            )
        if not isinstance(mask_raw, str) or not mask_raw.strip():
            return None, _rejection(
                source_format,
                source_ref,
                record_index,
                "E_MASK",
                "non-tombstone v2 rule requires mask",
                raw_text,
            )
        threshold = _coerce_text_int(threshold_raw)
        if threshold is None or threshold < 0:
            return None, _rejection(
                source_format,
                source_ref,
                record_index,
                "E_THRESHOLD",
                "non-tombstone v2 rule requires a non-negative integer threshold",
                raw_text,
            )
        field_path = canonical_field_path(field_path_raw)
        mask = canonical_mask_method(mask_raw)
        if not mask_method_allowed(mask, allowed_masks):
            return None, _rejection(
                source_format,
                source_ref,
                record_index,
                "E_MASK_NOT_ALLOWED",
                f"v2 mask {mask_raw!r} is not in the layout whitelist",
                raw_text,
            )

    return (
        RuleRecord(
            source_kind=source_kind,
            source_ref=source_ref,
            record_index=record_index,
            source_format=source_format,
            raw=raw_text,
            rule_name=rule_name.strip(),
            field_path=field_path,
            mask=mask,
            threshold=threshold,
            effective_ms=effective_ms,
            generation=generation,
            tombstone=tombstone,
            patch_id=patch_id,
        ),
        None,
    )


def _parse_v1_list(
    v1_value: Any,
    *,
    source_ref: str,
    observation_clock_ms: Optional[int],
    allowed_masks: Sequence[str],
    external_relative_seconds: Any,
    v1_generation: Any,
    start_index: int,
) -> Tuple[List[RuleRecord], List[Rejection], int]:
    records: List[RuleRecord] = []
    rejections: List[Rejection] = []
    entries: List[Any]
    if v1_value is None:
        return records, rejections, start_index
    if isinstance(v1_value, dict) and "lines" in v1_value:
        external_relative_seconds = v1_value.get(
            "relative_effective_seconds",
            external_relative_seconds,
        )
        if "generation" in v1_value:
            v1_generation = v1_value["generation"]
        entries = v1_value.get("lines") or []
    elif isinstance(v1_value, str):
        entries = v1_value.splitlines()
    elif isinstance(v1_value, list):
        entries = v1_value
    else:
        return records, [
            _rejection(
                "rules-metadata",
                source_ref,
                start_index,
                "E_V1_FIELDS",
                "v1 must be an array, object with lines, or newline text",
                v1_value,
            )
        ], start_index

    generation = _coerce_text_int(v1_generation)
    index = start_index
    for offset, entry in enumerate(entries):
        index += 1
        if isinstance(entry, dict):
            line = entry.get("line", entry.get("record"))
            if not isinstance(line, str):
                rejections.append(
                    _rejection(
                        "v1-pipe-line",
                        source_ref,
                        index,
                        "E_V1_FIELDS",
                        "v1 object entry must contain a string 'line'",
                        entry,
                    )
                )
                continue
        elif isinstance(entry, str):
            line = entry
        else:
            rejections.append(
                _rejection(
                    "v1-pipe-line",
                    source_ref,
                    index,
                    "E_V1_FIELDS",
                    "v1 entry must be a string or an object with a line",
                    entry,
                )
            )
            continue
        if not line.strip():
            rejections.append(
                _rejection(
                    "v1-pipe-line",
                    source_ref,
                    index,
                    "E_V1_FIELDS",
                    "v1 line must not be blank",
                    line,
                )
            )
            continue
        if generation is None or generation < 0:
            rejections.append(
                _rejection(
                    "v1-pipe-line",
                    source_ref,
                    index,
                    "E_GENERATION",
                    "v1 generation must be a non-negative integer",
                    line,
                )
            )
            continue
        if isinstance(external_relative_seconds, dict):
            first_field = line.split("|", 1)[0].strip()
            relative_source = external_relative_seconds.get(first_field)
        elif isinstance(external_relative_seconds, list):
            relative_source = (
                external_relative_seconds[offset]
                if offset < len(external_relative_seconds)
                else None
            )
        else:
            relative_source = external_relative_seconds
        relative_seconds, relative_error = _v1_relative_seconds(
            entry,
            relative_source,
        )
        if relative_error:
            rejections.append(
                _rejection(
                    "v1-pipe-line",
                    source_ref,
                    index,
                    "E_EFFECTIVE_SECONDS",
                    relative_error,
                    line,
                )
            )
            continue
        record, rejection = _parse_v1_record(
            line,
            source_ref=source_ref,
            record_index=index,
            observation_clock_ms=observation_clock_ms,
            relative_seconds=relative_seconds or 0,
            generation=generation,
            allowed_masks=allowed_masks,
        )
        if rejection:
            rejections.append(rejection)
        if record:
            records.append(record)
    return records, rejections, index


def _parse_v2_list(
    v2_value: Any,
    *,
    source_kind: str,
    source_ref: str,
    patch_id: Optional[str],
    allowed_masks: Sequence[str],
    start_index: int,
) -> Tuple[List[RuleRecord], List[Rejection], int]:
    records: List[RuleRecord] = []
    rejections: List[Rejection] = []
    if v2_value is None:
        return records, rejections, start_index
    entries: List[Any]
    if isinstance(v2_value, (dict, str)):
        entries = [v2_value]
    elif isinstance(v2_value, list):
        entries = v2_value
    else:
        return records, [
            _rejection(
                "rules-metadata",
                source_ref,
                start_index,
                "E_V2_FIELDS",
                "v2 must be an object, string, or array",
                v2_value,
            )
        ], start_index
    index = start_index
    for entry in entries:
        index += 1
        raw_override = entry if isinstance(entry, str) else None
        mapping = entry
        if isinstance(entry, str):
            mapping, error = _parse_v2_key_value_text(entry)
            if error:
                rejections.append(
                    _rejection(
                        "v2-key-value",
                        source_ref,
                        index,
                        "E_V2_FIELDS",
                        error,
                        entry,
                    )
                )
                continue
        record, rejection = _parse_v2_record(
            mapping,
            source_kind=source_kind,
            source_ref=source_ref,
            record_index=index,
            patch_id=patch_id,
            allowed_masks=allowed_masks,
            raw_override=raw_override,
        )
        if rejection:
            rejections.append(rejection)
        if record:
            records.append(record)
    return records, rejections, index


def _extract_clock(document: Dict[str, Any]) -> Optional[int]:
    for key in (
        "observation_clock_ms",
        "observed_clock_ms",
        "clock_ms",
    ):
        if key in document:
            value = document[key]
            if is_nonnegative_int(value):
                return value
            return None
    return None


def _extract_layout(document: Dict[str, Any]) -> Any:
    if "layout" in document:
        return document["layout"]
    if any(
        key in document
        for key in ("mask_methods", "threshold_mode", "layout_version")
    ):
        return {
            "mask_methods": document.get("mask_methods", list(DEFAULT_MASK_METHODS)),
            "threshold_mode": document.get("threshold_mode", "higher_wins"),
            "version": document.get("layout_version", "audit-mask-layout-v1"),
        }
    return None


def parse_rules_snapshot(path: str | Path) -> ParsedSnapshot:
    file_path = Path(path)
    source_ref = str(file_path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        rejection = _rejection(
            "rules-snapshot",
            source_ref,
            0,
            "E_IO",
            f"cannot read rules snapshot: {exc}",
        )
        return ParsedSnapshot(
            source_ref=source_ref,
            fatal_errors=[rejection],
            rejections=[rejection],
        )

    parsed = _parse_json_document(text)
    document: Dict[str, Any]
    if parsed is not None:
        if isinstance(parsed, dict):
            document = parsed
        elif isinstance(parsed, list):
            document = {"records": parsed}
        else:
            rejection = _rejection(
                "rules-snapshot",
                source_ref,
                1,
                "E_SNAPSHOT",
                "rules snapshot root must be an object or array",
                text,
            )
            return ParsedSnapshot(
                source_ref=source_ref,
                fatal_errors=[rejection],
                rejections=[rejection],
            )
    else:
        document = _parse_rules_jsonl(text, source_ref)

    layout, layout_rejections = _parse_layout(
        _extract_layout(document),
        source_ref,
        1,
    )
    result = ParsedSnapshot(
        source_ref=source_ref,
        layout=layout,
        rejections=list(layout_rejections),
        fatal_errors=list(layout_rejections) if layout is None else [],
    )
    if layout is None:
        return result

    clock = _extract_clock(document)
    if clock is None and any(
        key in document
        for key in ("observation_clock_ms", "observed_clock_ms", "clock_ms")
    ):
        rejection = _rejection(
            "rules-metadata",
            source_ref,
            1,
            "E_RULE_CLOCK",
            "observation clock must be a non-negative integer",
            document,
        )
        result.rejections.append(rejection)
    result.observation_clock_ms = clock
    snapshot_id = document.get("snapshot_id", document.get("id"))
    if snapshot_id is not None and (
        not isinstance(snapshot_id, str) or not snapshot_id.strip()
    ):
        result.rejections.append(
            _rejection(
                "rules-metadata",
                source_ref,
                1,
                "E_SNAPSHOT_ID",
                "snapshot_id, when present, must be a non-empty string",
                document,
            )
        )
        snapshot_id = None
    result.snapshot_id = snapshot_id

    defaults, default_rejections = _parse_category_defaults(
        document.get("category_defaults", document.get("defaults")),
        layout,
        source_ref,
        1,
    )
    result.defaults = defaults
    result.rejections.extend(default_rejections)

    index = 0
    records, rejections, index = _parse_v1_list(
        document.get("v1"),
        source_ref=source_ref,
        observation_clock_ms=clock,
        allowed_masks=layout.mask_methods,
        external_relative_seconds=document.get("v1_relative_seconds"),
        v1_generation=document.get("v1_generation", 1),
        start_index=index,
    )
    result.records.extend(records)
    result.rejections.extend(rejections)

    v2_records, rejections, index = _parse_v2_list(
        document.get("v2"),
        source_kind="snapshot",
        source_ref=source_ref,
        patch_id=None,
        allowed_masks=layout.mask_methods,
        start_index=index,
    )
    result.records.extend(v2_records)
    result.rejections.extend(rejections)

    mixed = document.get("records")
    if mixed is not None:
        if not isinstance(mixed, list):
            result.rejections.append(
                _rejection(
                    "rules-metadata",
                    source_ref,
                    index + 1,
                    "E_RECORDS",
                    "records must be an array",
                    mixed,
                )
            )
        else:
            for entry in mixed:
                index += 1
                if (
                    isinstance(entry, str)
                    and entry.count("|") == 3
                    and "=" not in entry
                ):
                    relative_seconds, relative_error = _v1_relative_seconds(
                        entry,
                        None,
                    )
                    if relative_error:
                        result.rejections.append(
                            _rejection(
                                "v1-pipe-line",
                                source_ref,
                                index,
                                "E_EFFECTIVE_SECONDS",
                                relative_error,
                                entry,
                            )
                        )
                        continue
                    generation = _coerce_text_int(
                        document.get("v1_generation", 1)
                    )
                    if generation is None or generation < 0:
                        result.rejections.append(
                            _rejection(
                                "v1-pipe-line",
                                source_ref,
                                index,
                                "E_GENERATION",
                                "v1 generation must be a non-negative integer",
                                entry,
                            )
                        )
                        continue
                    record, rejection = _parse_v1_record(
                        entry,
                        source_ref=source_ref,
                        record_index=index,
                        observation_clock_ms=clock,
                        relative_seconds=relative_seconds or 0,
                        generation=generation,
                        allowed_masks=layout.mask_methods,
                    )
                elif isinstance(entry, dict):
                    record, rejection = _parse_v2_record(
                        entry,
                        source_kind="snapshot",
                        source_ref=source_ref,
                        record_index=index,
                        patch_id=None,
                        allowed_masks=layout.mask_methods,
                    )
                elif isinstance(entry, str):
                    mapping, error = _parse_v2_key_value_text(entry)
                    if error:
                        record = None
                        rejection = _rejection(
                            "v2-key-value",
                            source_ref,
                            index,
                            "E_V2_FIELDS",
                            error,
                            entry,
                        )
                    else:
                        record, rejection = _parse_v2_record(
                            mapping,
                            source_kind="snapshot",
                            source_ref=source_ref,
                            record_index=index,
                            patch_id=None,
                            allowed_masks=layout.mask_methods,
                            raw_override=entry,
                        )
                else:
                    record = None
                    rejection = _rejection(
                        "rules-snapshot",
                        source_ref,
                        index,
                        "E_RECORD",
                        "mixed rule record must be a v1 line, v2 kv line, or object",
                        entry,
                    )
                if rejection:
                    result.rejections.append(rejection)
                if record:
                    result.records.append(record)

    result.explicit_empty = bool(
        document.get("allow_empty", document.get("explicit_empty", False))
    )
    if not result.records and result.rejections and not result.explicit_empty:
        fatal_empty = _rejection(
            "rules-snapshot",
            source_ref,
            1,
            "E_SNAPSHOT_EMPTY",
            "snapshot has no valid rules after parse failures",
            document,
        )
        result.fatal_errors.append(fatal_empty)
        result.rejections.append(fatal_empty)
    return result


def _parse_rules_jsonl(text: str, source_ref: str) -> Dict[str, Any]:
    document: Dict[str, Any] = {}
    v1_lines: List[str] = []
    v2_records: List[Any] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        parsed = _parse_json_document(stripped)
        if isinstance(parsed, dict):
            if "record" in parsed and isinstance(parsed["record"], str):
                v1_lines.append(parsed["record"])
            elif "v2" in parsed or "records" in parsed or "v1" in parsed:
                for key in (
                    "observation_clock_ms",
                    "observed_clock_ms",
                    "clock_ms",
                    "layout",
                    "layout_version",
                    "mask_methods",
                    "threshold_mode",
                    "category_defaults",
                    "snapshot_id",
                    "v1_generation",
                    "v1_relative_seconds",
                ):
                    if key in parsed:
                        document[key] = parsed[key]
                for entry in parsed.get("v1", []):
                    v1_lines.append(entry)
                v2_records.extend(parsed.get("v2", []))
            elif any(
                alias in parsed
                for aliases in RULE_ALIASES.values()
                for alias in aliases
            ):
                v2_records.append(parsed)
            else:
                for key in (
                    "observation_clock_ms",
                    "observed_clock_ms",
                    "clock_ms",
                    "layout",
                    "layout_version",
                    "mask_methods",
                    "threshold_mode",
                    "category_defaults",
                    "snapshot_id",
                    "v1_generation",
                    "v1_relative_seconds",
                ):
                    if key in parsed:
                        document[key] = parsed[key]
        elif isinstance(parsed, list):
            v2_records.extend(parsed)
        elif stripped.count("|") == 3 and "=" not in stripped:
            v1_lines.append(stripped)
        else:
            v2_records.append(stripped)
    if v1_lines:
        document["v1"] = v1_lines
    if v2_records:
        document["v2"] = v2_records
    return document


def parse_patch_file(path: str | Path) -> ParsedPatchFile:
    file_path = Path(path)
    source_ref = str(file_path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        rejection = _rejection(
            "rules-patch",
            source_ref,
            0,
            "E_IO",
            f"cannot read patch file: {exc}",
        )
        return ParsedPatchFile(
            source_ref=source_ref,
            fatal_errors=[rejection],
            rejections=[rejection],
        )
    parsed = _parse_json_document(text)
    if parsed is None:
        rejection = _rejection(
            "rules-patch",
            source_ref,
            1,
            "E_PATCH",
            "patch file must be valid JSON",
            text,
        )
        return ParsedPatchFile(
            source_ref=source_ref,
            fatal_errors=[rejection],
            rejections=[rejection],
        )
    if isinstance(parsed, list):
        patch_docs = parsed
        document: Dict[str, Any] = {}
    elif isinstance(parsed, dict):
        document = parsed
        raw_patches = parsed.get("patches")
        if isinstance(raw_patches, list):
            patch_docs = raw_patches
        elif "patch_id" in parsed or "records" in parsed or "v2" in parsed:
            patch_docs = [parsed]
        else:
            rejection = _rejection(
                "rules-patch",
                source_ref,
                1,
                "E_PATCH",
                "patch document must contain a patches array or a patch object",
                parsed,
            )
            return ParsedPatchFile(
                source_ref=source_ref,
                fatal_errors=[rejection],
                rejections=[rejection],
            )
    else:
        rejection = _rejection(
            "rules-patch",
            source_ref,
            1,
            "E_PATCH",
            "patch root must be an object or array",
            parsed,
        )
        return ParsedPatchFile(
            source_ref=source_ref,
            fatal_errors=[rejection],
            rejections=[rejection],
        )

    result = ParsedPatchFile(source_ref=source_ref)
    layout, layout_rejections = _parse_layout(
        _extract_layout(document),
        source_ref,
        1,
    )
    if layout is None:
        result.fatal_errors.extend(layout_rejections)
        result.rejections.extend(layout_rejections)
        return result
    clock = _extract_clock(document)

    for patch_index, patch_doc in enumerate(patch_docs, start=1):
        if not isinstance(patch_doc, dict):
            result.rejections.append(
                _rejection(
                    "rules-patch",
                    source_ref,
                    patch_index,
                    "E_PATCH",
                    "patch entry must be an object",
                    patch_doc,
                )
            )
            continue
        patch_id = patch_doc.get("patch_id", patch_doc.get("id"))
        if not isinstance(patch_id, str) or not patch_id.strip():
            result.rejections.append(
                _rejection(
                    "rules-patch",
                    source_ref,
                    patch_index,
                    "E_PATCH_ID",
                    "patch_id must be a non-empty string",
                    patch_doc,
                )
            )
            continue

        changed_raw = patch_doc.get(
            "changed_rules",
            patch_doc.get("changed", []),
        )
        tombstones_raw = patch_doc.get(
            "tombstones",
            patch_doc.get("tombstoned_rules", []),
        )
        if changed_raw is None:
            changed_raw = []
        if tombstones_raw is None:
            tombstones_raw = []
        if not isinstance(changed_raw, list) or not isinstance(
            tombstones_raw,
            list,
        ):
            result.rejections.append(
                _rejection(
                    "rules-patch",
                    source_ref,
                    patch_index,
                    "E_PATCH_DECLARATION",
                    "changed_rules and tombstones must be arrays",
                    patch_doc,
                )
            )
            continue
        changed_rules: List[str] = []
        tombstones: List[str] = []
        declaration_ok = True
        for item in changed_raw:
            if not isinstance(item, str) or not item.strip():
                declaration_ok = False
                break
            changed_rules.append(item.strip())
        for item in tombstones_raw:
            if not isinstance(item, str) or not item.strip():
                declaration_ok = False
                break
            tombstones.append(item.strip())
        if not declaration_ok:
            result.rejections.append(
                _rejection(
                    "rules-patch",
                    source_ref,
                    patch_index,
                    "E_PATCH_DECLARATION",
                    "rule names in changed_rules/tombstones must be non-empty strings",
                    patch_doc,
                )
            )
            continue

        patch_layout = layout
        patch_layout_value = _extract_layout(patch_doc)
        if patch_layout_value is not None:
            patch_layout, patch_layout_rejections = _parse_layout(
                patch_layout_value,
                source_ref,
                patch_index,
            )
            if patch_layout is None:
                result.rejections.extend(patch_layout_rejections)
                continue
        patch_clock = _extract_clock(patch_doc)
        if patch_clock is None:
            patch_clock = clock

        records: List[RuleRecord] = []
        patch_rejections: List[Rejection] = []
        inner_index = 0
        v1_records, v1_rejections, inner_index = _parse_v1_list(
            patch_doc.get("v1"),
            source_ref=source_ref,
            observation_clock_ms=patch_clock,
            allowed_masks=patch_layout.mask_methods,
            external_relative_seconds=patch_doc.get("v1_relative_seconds"),
            v1_generation=patch_doc.get("v1_generation", 1),
            start_index=inner_index,
        )
        records.extend(v1_records)
        patch_rejections.extend(v1_rejections)
        v2_records, v2_rejections, inner_index = _parse_v2_list(
            patch_doc.get("v2"),
            source_kind="patch",
            source_ref=source_ref,
            patch_id=patch_id,
            allowed_masks=patch_layout.mask_methods,
            start_index=inner_index,
        )
        records.extend(v2_records)
        patch_rejections.extend(v2_rejections)

        raw_records = patch_doc.get("records")
        if raw_records is not None:
            if not isinstance(raw_records, list):
                patch_rejections.append(
                    _rejection(
                        "rules-patch",
                        source_ref,
                        patch_index,
                        "E_PATCH_RECORDS",
                        "patch records must be an array",
                        patch_doc,
                    )
                )
            else:
                for entry in raw_records:
                    inner_index += 1
                    if (
                        isinstance(entry, str)
                        and entry.count("|") == 3
                        and "=" not in entry
                    ):
                        relative_seconds, relative_error = _v1_relative_seconds(
                            entry,
                            None,
                        )
                        if relative_error:
                            patch_rejections.append(
                                _rejection(
                                    "v1-pipe-line",
                                    source_ref,
                                    inner_index,
                                    "E_EFFECTIVE_SECONDS",
                                    relative_error,
                                    entry,
                                )
                            )
                            continue
                        v1_generation = _coerce_text_int(
                            patch_doc.get("v1_generation", 1)
                        )
                        if v1_generation is None or v1_generation < 0:
                            patch_rejections.append(
                                _rejection(
                                    "v1-pipe-line",
                                    source_ref,
                                    inner_index,
                                    "E_GENERATION",
                                    "v1 generation must be a non-negative integer",
                                    entry,
                                )
                            )
                            continue
                        record, rejection = _parse_v1_record(
                            entry,
                            source_ref=source_ref,
                            record_index=inner_index,
                            observation_clock_ms=patch_clock,
                            relative_seconds=relative_seconds or 0,
                            generation=v1_generation,
                            allowed_masks=patch_layout.mask_methods,
                        )
                    elif isinstance(entry, dict):
                        record, rejection = _parse_v2_record(
                            entry,
                            source_kind="patch",
                            source_ref=source_ref,
                            record_index=inner_index,
                            patch_id=patch_id,
                            allowed_masks=patch_layout.mask_methods,
                        )
                    elif isinstance(entry, str):
                        mapping, error = _parse_v2_key_value_text(entry)
                        if error:
                            record = None
                            rejection = _rejection(
                                "v2-key-value",
                                source_ref,
                                inner_index,
                                "E_V2_FIELDS",
                                error,
                                entry,
                            )
                        else:
                            record, rejection = _parse_v2_record(
                                mapping,
                                source_kind="patch",
                                source_ref=source_ref,
                                record_index=inner_index,
                                patch_id=patch_id,
                                allowed_masks=patch_layout.mask_methods,
                                raw_override=entry,
                            )
                    else:
                        record = None
                        rejection = _rejection(
                            "rules-patch",
                            source_ref,
                            inner_index,
                            "E_PATCH_RECORD",
                            "patch record must be a v1 line, v2 kv line, or object",
                            entry,
                        )
                    if rejection:
                        patch_rejections.append(rejection)
                    if record:
                        records.append(record)

        if not records and not changed_rules and not tombstones:
            patch_rejections.append(
                _rejection(
                    "rules-patch",
                    source_ref,
                    patch_index,
                    "E_PATCH_DECLARATION",
                    "patch must declare changed/tombstoned names or contain records",
                    patch_doc,
                )
            )
            result.rejections.extend(patch_rejections)
            continue

        record_names = {record.rule_name for record in records}
        for name in sorted(record_names):
            if name not in changed_rules and name not in tombstones:
                changed_rules.append(name)
        for record in records:
            if record.tombstone and record.rule_name not in tombstones:
                tombstones.append(record.rule_name)
        result.patches.append(
            ParsedPatch(
                patch_id=patch_id.strip(),
                source_ref=source_ref,
                changed_rules=sorted(set(changed_rules)),
                tombstones=sorted(set(tombstones)),
                records=records,
                rejections=patch_rejections,
                raw=canonical_json(patch_doc),
            )
        )
        result.rejections.extend(patch_rejections)
    return result
