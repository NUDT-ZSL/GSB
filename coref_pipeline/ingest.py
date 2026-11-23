"""Deterministic JSONL ingestion, validation, and normalization."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .cache import CacheStore, cache_key
from .canonical import content_hash, q6
from .config import RulesConfig
from .errors import (
    E_DUP_EVENT,
    E_EVIDENCE_CONFIG,
    E_INPUT_INVALID,
    E_OFFSET_INVALID,
    E_ROLE_INVALID,
    E_TYPE_INVALID,
    PipelineError,
)


INGEST_SCHEMA = "normalized-event/v1"


@dataclass
class IngestionResult:
    events: list[dict[str, Any]]
    event_hashes: dict[str, str]
    event_digest: str
    input_roles: list[str]
    input_event_types: list[str]
    batch_numbers: list[int]
    cache_hit_event_ids: list[str]
    cache_miss_event_ids: list[str]

    def batch_events(self) -> dict[int, list[dict[str, Any]]]:
        grouped: dict[int, list[dict[str, Any]]] = {}
        for event in self.events:
            grouped.setdefault(event["batch_no"], []).append(event)
        return grouped

    def summary(self) -> dict[str, Any]:
        return {
            "event_count": len(self.events),
            "batch_numbers": self.batch_numbers,
            "event_digest": self.event_digest,
            "batches": [
                {
                    "batch_no": batch_no,
                    "event_count": len(events),
                    "event_ids": [event["event_id"] for event in events],
                }
                for batch_no, events in sorted(self.batch_events().items())
            ],
            "cache": {
                "hit_event_ids": self.cache_hit_event_ids,
                "miss_event_ids": self.cache_miss_event_ids,
            },
        }


def _event_error(
    code: str,
    event_id: str | None,
    field: str,
    message: str,
    **details: Any,
) -> PipelineError:
    return PipelineError(
        code=code,
        stage="ingest",
        event_id=event_id,
        field=field,
        message=message,
        details=details,
    )


def read_controlled_set(path: Path, key: str) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="ingest",
            field=key,
            message=f"无法读取受控表 {path.name}: {exc}",
        ) from exc
    values: Any
    if key == "event_types":
        values = payload.get("event_types")
    elif key == "roles":
        roles = payload.get("roles", {})
        values = list(roles) if isinstance(roles, dict) else roles
    else:
        values = payload.get(key)
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="ingest",
            field=key,
            message=f"受控表 {path.name} 的 {key} 必须为字符串数组",
        )
    return sorted(set(values))


def _required_int(value: Any, event_id: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _event_error(E_INPUT_INVALID, event_id, field, "必须为整数")
    return value


def _normalize_raw_event(
    raw: dict[str, Any],
    controlled_types: set[str],
    controlled_roles: set[str],
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _event_error(E_INPUT_INVALID, None, "$", "事件记录必须为 JSON 对象")
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise _event_error(E_INPUT_INVALID, None, "event_id", "event_id 必须为非空字符串")

    batch_no = _required_int(raw.get("batch_no"), event_id, "batch_no")
    if batch_no < 1:
        raise _event_error(E_INPUT_INVALID, event_id, "batch_no", "batch_no 必须大于等于 1")
    doc_id = raw.get("doc_id")
    if not isinstance(doc_id, str) or not doc_id.strip():
        raise _event_error(E_INPUT_INVALID, event_id, "doc_id", "doc_id 必须为非空字符串")

    event_type = raw.get("event_type")
    if not isinstance(event_type, str) or event_type not in controlled_types:
        raise _event_error(
            E_TYPE_INVALID,
            event_id,
            "event_type",
            f"event_type 不在受控集合内: {event_type!r}",
            value=event_type,
        )

    entities_raw = raw.get("entities")
    if not isinstance(entities_raw, list) or not entities_raw:
        raise _event_error(
            E_INPUT_INVALID, event_id, "entities", "entities 必须为非空数组"
        )
    entities: list[dict[str, str]] = []
    for index, entity in enumerate(entities_raw):
        if not isinstance(entity, dict):
            raise _event_error(
                E_INPUT_INVALID, event_id, f"entities[{index}]", "实体必须为对象"
            )
        name = entity.get("name")
        role = entity.get("role")
        if not isinstance(name, str) or not name.strip():
            raise _event_error(
                E_INPUT_INVALID,
                event_id,
                f"entities[{index}].name",
                "实体名必须为非空字符串",
            )
        if not isinstance(role, str) or role not in controlled_roles:
            raise _event_error(
                E_ROLE_INVALID,
                event_id,
                f"entities[{index}].role",
                f"实体角色不在受控角色表中: {role!r}",
                role=role,
            )
        entities.append({"name": name.strip(), "role": role})
    entities.sort(key=lambda item: (item["role"], item["name"]))

    time_expr = raw.get("time_expr")
    if not isinstance(time_expr, str) or not time_expr.strip():
        raise _event_error(
            E_INPUT_INVALID, event_id, "time_expr", "time_expr 必须为非空字符串"
        )

    mentions_raw = raw.get("mentions")
    if not isinstance(mentions_raw, list) or not mentions_raw:
        raise _event_error(
            E_INPUT_INVALID, event_id, "mentions", "mentions 必须为非空数组"
        )
    mentions: list[dict[str, Any]] = []
    for index, mention in enumerate(mentions_raw):
        field = f"mentions[{index}]"
        if not isinstance(mention, dict):
            raise _event_error(E_INPUT_INVALID, event_id, field, "mention 必须为对象")
        trigger = mention.get("trigger")
        if not isinstance(trigger, str) or not trigger:
            raise _event_error(
                E_INPUT_INVALID, event_id, f"{field}.trigger", "触发词必须为非空字符串"
            )
        start = mention.get("start")
        end = mention.get("end")
        if (
            isinstance(start, bool)
            or isinstance(end, bool)
            or not isinstance(start, int)
            or not isinstance(end, int)
        ):
            raise _event_error(
                E_OFFSET_INVALID,
                event_id,
                f"{field}.start/end",
                "码点偏移必须为整数",
                start=start,
                end=end,
                trigger_length=len(trigger),
            )
        if start < 0 or end <= start or end > len(trigger):
            raise _event_error(
                E_OFFSET_INVALID,
                event_id,
                f"{field}.start/end",
                "码点偏移越出触发词长度",
                start=start,
                end=end,
                trigger_length=len(trigger),
            )
        mentions.append({"trigger": trigger, "start": start, "end": end})
    mentions.sort(key=lambda item: (item["start"], item["end"], item["trigger"]))

    source_trust = raw.get("source_trust")
    if (
        isinstance(source_trust, bool)
        or not isinstance(source_trust, (int, float))
        or not math.isfinite(float(source_trust))
        or float(source_trust) < 0
        or float(source_trust) > 1
    ):
        raise _event_error(
            E_INPUT_INVALID,
            event_id,
            "source_trust",
            "source_trust 必须位于 [0, 1]",
            value=source_trust,
        )

    return {
        "schema_version": INGEST_SCHEMA,
        "event_id": event_id,
        "doc_id": doc_id,
        "batch_no": batch_no,
        "event_type": event_type,
        "entities": entities,
        "time_expr": time_expr.strip(),
        "mentions": mentions,
        "source_trust": q6(float(source_trust)),
    }


def load_events_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="ingest",
            field="events.jsonl",
            message=f"无法读取事件清单: {exc}",
        ) from exc
    events: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            event_id = None
            try:
                partial = json.loads(line[: max(1, exc.pos)])
                if isinstance(partial, dict):
                    event_id = partial.get("event_id")
            except (ValueError, TypeError):
                pass
            raise PipelineError(
                code=E_INPUT_INVALID,
                stage="ingest",
                event_id=event_id if isinstance(event_id, str) else None,
                field=f"line:{line_no}",
                message=f"JSONL 解析失败: {exc.msg}",
            ) from exc
        if not isinstance(raw, dict):
            raise PipelineError(
                code=E_INPUT_INVALID,
                stage="ingest",
                field=f"line:{line_no}",
                message="JSONL 每行必须为对象",
            )
        events.append(raw)
    return events


def ingest_events(
    raw_events: Iterable[dict[str, Any]],
    *,
    config: RulesConfig,
    controlled_event_types: Iterable[str],
    controlled_roles: Iterable[str],
    cache: CacheStore | None = None,
) -> IngestionResult:
    controlled_types = set(controlled_event_types)
    controlled_role_set = set(controlled_roles)
    roles = controlled_role_set | set(config.role_weights)
    if not controlled_types:
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="ingest",
            field="event_types",
            message="事件类型受控集合不得为空",
        )
    if not roles:
        raise PipelineError(
            code=E_INPUT_INVALID,
            stage="ingest",
            field="roles",
            message="实体角色受控集合不得为空",
        )
    missing_role_weights = sorted(controlled_role_set - set(config.role_weights))
    if missing_role_weights:
        raise PipelineError(
            code=E_EVIDENCE_CONFIG,
            stage="evidence_config",
            field=f"entity_role_weights.{missing_role_weights[0]}",
            message="受控角色缺少实体角色权重",
            details={"missing_roles": missing_role_weights},
        )

    normalized: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    event_hashes: dict[str, str] = {}
    cache_hits: list[str] = []
    cache_misses: list[str] = []
    table_hash = content_hash(
        {"event_types": sorted(controlled_types), "roles": sorted(roles)}
    )
    for index, raw in enumerate(raw_events):
        event_id_hint = raw.get("event_id") if isinstance(raw, dict) else None
        record = _normalize_raw_event(raw, controlled_types, controlled_role_set)
        event_id = record["event_id"]
        if event_id in seen:
            raise _event_error(
                E_DUP_EVENT,
                event_id,
                "event_id",
                "event_id 重复",
                first_index=seen[event_id],
                duplicate_index=index,
            )
        seen[event_id] = index
        key = cache_key(
            "normalized-event/v1",
            {
                "raw": record,
                "tables": table_hash,
            },
        )
        cached = cache.get("normalized_event", key) if cache is not None else None
        if cached is not None and cached.get("record") == record:
            cache_hits.append(event_id)
        else:
            cache_misses.append(event_id)
            if cache is not None:
                cache.put("normalized_event", key, {"record": record})
        digest = content_hash(record)
        normalized.append(record)
        event_hashes[event_id] = digest

    normalized.sort(key=lambda item: (item["batch_no"], item["event_id"]))
    event_digest = content_hash(
        [{key: value for key, value in event.items()} for event in normalized]
    )
    return IngestionResult(
        events=normalized,
        event_hashes=event_hashes,
        event_digest=event_digest,
        input_roles=sorted(roles),
        input_event_types=sorted(controlled_types),
        batch_numbers=sorted({event["batch_no"] for event in normalized}),
        cache_hit_event_ids=sorted(cache_hits),
        cache_miss_event_ids=sorted(cache_misses),
    )

