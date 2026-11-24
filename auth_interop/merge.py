from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import MERGE_POLICY_VERSION
from .codes import E_PKCE_DOWNGRADE, E_STATE_CONFLICT
from .model import NormalizedRecord, record_entity_key


JsonDict = dict[str, Any]


@dataclass
class MergedEntity:
    entity_key: str
    entity_type: str
    records: list[NormalizedRecord] = field(default_factory=list)
    authoritative: Optional[NormalizedRecord] = None
    status: str = "active"
    conflicts: list[JsonDict] = field(default_factory=list)
    findings: list[JsonDict] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "entity_key": self.entity_key,
            "entity_type": self.entity_type,
            "status": self.status,
            "authoritative": self.authoritative.to_dict() if self.authoritative else None,
            "records": [record.to_dict() for record in self.records],
            "conflicts": self.conflicts,
            "findings": self.findings,
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> "MergedEntity":
        records = [NormalizedRecord.from_dict(item) for item in data.get("records", [])]
        authoritative_data = data.get("authoritative")
        authoritative = (
            NormalizedRecord.from_dict(authoritative_data) if authoritative_data else None
        )
        return cls(
            entity_key=data["entity_key"],
            entity_type=data.get("entity_type", "unknown"),
            records=records,
            authoritative=authoritative,
            status=data.get("status", "active"),
            conflicts=list(data.get("conflicts", [])),
            findings=list(data.get("findings", [])),
        )


@dataclass
class MergedView:
    policy_version: str = MERGE_POLICY_VERSION
    entities: dict[str, MergedEntity] = field(default_factory=dict)
    rejections: list[JsonDict] = field(default_factory=list)
    conflicts: list[JsonDict] = field(default_factory=list)
    pkce_findings: list[JsonDict] = field(default_factory=list)
    applied_batches: list[str] = field(default_factory=list)

    def to_dict(self) -> JsonDict:
        return {
            "policy_version": self.policy_version,
            "entities": {
                key: entity.to_dict() for key, entity in sorted(self.entities.items())
            },
            "rejections": self.rejections,
            "conflicts": self.conflicts,
            "pkce_findings": self.pkce_findings,
            "applied_batches": self.applied_batches,
            "stats": self.stats(),
        }

    @classmethod
    def from_dict(cls, data: JsonDict) -> "MergedView":
        entities = {
            key: MergedEntity.from_dict(value)
            for key, value in data.get("entities", {}).items()
        }
        return cls(
            policy_version=data.get("policy_version", "unknown"),
            entities=entities,
            rejections=list(data.get("rejections", [])),
            conflicts=list(data.get("conflicts", [])),
            pkce_findings=list(data.get("pkce_findings", [])),
            applied_batches=list(data.get("applied_batches", [])),
        )

    def stats(self) -> JsonDict:
        statuses: dict[str, int] = {}
        for entity in self.entities.values():
            statuses[entity.status] = statuses.get(entity.status, 0) + 1
        return {
            "entity_total": len(self.entities),
            "statuses": dict(sorted(statuses.items())),
            "rejection_total": len(self.rejections),
            "conflict_total": len(self.conflicts),
            "pkce_finding_total": len(self.pkce_findings),
            "batch_total": len(self.applied_batches),
        }

    def code_index(self) -> dict[str, list[str]]:
        index: dict[str, list[str]] = {}
        for key, entity in self.entities.items():
            if entity.entity_type != "authorization_code" or not entity.authoritative:
                continue
            code = entity.authoritative.code
            if code:
                index.setdefault(code, []).append(key)
        return {code: sorted(keys) for code, keys in sorted(index.items())}

    def active_keys(self) -> list[str]:
        return sorted(key for key, entity in self.entities.items() if entity.status == "active")


def new_view(rejections: Optional[list[Any]] = None) -> MergedView:
    view = MergedView()
    if rejections:
        view.rejections = [
            item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in rejections
        ]
    return view


def apply_batch(view: MergedView, batch_id: str, records: list[NormalizedRecord]) -> MergedView:
    for record in records:
        key = record_entity_key(record)
        entity = view.entities.get(key)
        if entity is None:
            entity = MergedEntity(entity_key=key, entity_type=record.record_type)
            view.entities[key] = entity
        entity.records.append(record)

    if batch_id not in view.applied_batches:
        view.applied_batches.append(batch_id)

    recompute(view)
    return view


def recompute(view: MergedView) -> None:
    view.conflicts.clear()
    view.pkce_findings.clear()
    for entity in view.entities.values():
        entity.conflicts.clear()
        entity.findings.clear()
        ordered = sorted(entity.records, key=authority_tuple)
        entity.authoritative = ordered[-1] if ordered else None
        if not entity.authoritative:
            entity.status = "empty"
            continue

        if entity.authoritative.tombstoned:
            entity.status = "tombstoned"
        else:
            entity.status = "active"
            _detect_field_conflicts(entity, ordered)
            if entity.conflicts:
                entity.status = "conflict"

        _detect_pkce_lineage(entity, ordered)
        view.conflicts.extend(entity.conflicts)
        view.pkce_findings.extend(entity.findings)

    _detect_global_code_client_conflicts(view)
    view.conflicts.sort(key=lambda item: (item.get("entity_key", ""), item.get("field", "")))
    view.pkce_findings.sort(
        key=lambda item: (item.get("entity_key", ""), item.get("reason", ""))
    )


def authority_tuple(record: NormalizedRecord) -> tuple[int, int, int, str, int]:
    source_rank = 2 if record.source.source_version == "v2" else 1
    return (
        int(record.generation or 0),
        int(record.as_of_ms or 0),
        source_rank,
        record.source.path,
        record.source.record_no,
    )


def _source_side(record: NormalizedRecord) -> JsonDict:
    source = record.source_dict()
    source["authority_rank"] = authority_tuple(record)[:3]
    return source


def _add_conflict(
    entity: MergedEntity,
    field_name: str,
    left: Any,
    right: Any,
    left_record: NormalizedRecord,
    right_record: NormalizedRecord,
    message: str,
) -> None:
    conflict = {
        "code": E_STATE_CONFLICT,
        "entity_key": entity.entity_key,
        "field": field_name,
        "left": left,
        "right": right,
        "left_source": _source_side(left_record),
        "right_source": _source_side(right_record),
        "message": message,
    }
    entity.conflicts.append(conflict)


def _detect_field_conflicts(
    entity: MergedEntity, ordered: list[NormalizedRecord]
) -> None:
    authoritative = ordered[-1]
    for record in ordered[:-1]:
        if record.tombstoned:
            continue

        if (
            record.client_id
            and authoritative.client_id
            and record.client_id != authoritative.client_id
        ):
            _add_conflict(
                entity,
                "client_id",
                record.client_id,
                authoritative.client_id,
                record,
                authoritative,
                "client_id differs between record generations",
            )

        old_scope = sorted(set(record.scope or []))
        new_scope = sorted(set(authoritative.scope or []))
        if old_scope != new_scope:
            _add_conflict(
                entity,
                "scope",
                old_scope,
                new_scope,
                record,
                authoritative,
                "normalized scope sets differ between record generations",
            )

        if record.expires_at_ms != authoritative.expires_at_ms:
            _add_conflict(
                entity,
                "expires_at_ms",
                record.expires_at_ms,
                authoritative.expires_at_ms,
                record,
                authoritative,
                "expiry instant differs between record generations",
            )


def _detect_pkce_lineage(
    entity: MergedEntity, ordered: list[NormalizedRecord]
) -> None:
    for previous, current in zip(ordered, ordered[1:]):
        prev_challenge = bool(previous.pkce_challenge)
        curr_challenge = bool(current.pkce_challenge)
        reason: Optional[str] = None
        message = ""

        if (
            previous.source.source_version == "v1"
            and not prev_challenge
            and current.source.source_version == "v2"
            and curr_challenge
        ):
            reason = "legacy_no_pkce_replaced_by_pkce"
            message = (
                "v1 no-PKCE record was wholly replaced by a newer v2 PKCE record"
            )
        elif prev_challenge and not curr_challenge:
            reason = "pkce_weakened"
            message = "PKCE challenge present in an earlier generation is absent in a newer record"

        if reason:
            entity.findings.append(
                {
                    "code": E_PKCE_DOWNGRADE,
                    "entity_key": entity.entity_key,
                    "reason": reason,
                    "message": message,
                    "before_generation": previous.generation,
                    "after_generation": current.generation,
                    "before_source": _source_side(previous),
                    "after_source": _source_side(current),
                }
            )


def _detect_global_code_client_conflicts(view: MergedView) -> None:
    owners: dict[str, list[NormalizedRecord]] = {}
    for entity in view.entities.values():
        for record in entity.records:
            if (
                record.record_type == "authorization_code"
                and record.code
                and not record.tombstoned
            ):
                owners.setdefault(record.code, []).append(record)

    for code, records in owners.items():
        clients = {record.client_id for record in records if record.client_id}
        if len(clients) <= 1:
            continue
        ordered = sorted(records, key=authority_tuple)
        left, right = ordered[0], ordered[-1]
        entity_key = f"code-ownership:{code}"
        conflict = {
            "code": E_STATE_CONFLICT,
            "entity_key": entity_key,
            "field": "client_id",
            "left": left.client_id,
            "right": right.client_id,
            "left_source": _source_side(left),
            "right_source": _source_side(right),
            "message": f"authorization code {code!r} is assigned to multiple clients",
        }
        view.conflicts.append(conflict)
        target = view.entities.get(record_entity_key(right))
        if target is not None and not any(
            item.get("entity_key") == entity_key and item.get("field") == "client_id"
            for item in target.conflicts
        ):
            target.conflicts.append(conflict)
