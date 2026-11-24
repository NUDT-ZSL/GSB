"""Unified policy generation merge and conflict detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .canonical import stable_hash
from .codes import BusinessCode as Codes
from .models import PolicyRecord


@dataclass
class PolicyView:
    policies: list[PolicyRecord] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    tombstone_overrides: list[dict[str, Any]] = field(default_factory=list)
    records_by_key: dict[str, list[PolicyRecord]] = field(default_factory=dict)

    @property
    def policy_by_key(self) -> dict[str, PolicyRecord]:
        return {item.policy_key: item for item in self.policies}

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_count": len(self.policies),
            "conflict_count": len(self.conflicts),
            "tombstone_override_count": len(self.tombstone_overrides),
            "policies": [policy_summary(item) for item in sorted(self.policies, key=lambda p: p.policy_key)],
            "conflicts": self.conflicts,
            "tombstone_overrides": self.tombstone_overrides,
        }


def policy_content_fingerprint(record: PolicyRecord) -> str:
    payload = {
        "policy_key": record.policy_key,
        "generation": record.generation,
        "effective_start_ms": record.effective_start_ms,
        "effective_end_ms": record.effective_end_ms,
        "tombstone": record.tombstone,
        "action_code": record.action_code,
        "match_digest": record.match_digest,
    }
    return stable_hash(payload)


def _precedence(record: PolicyRecord) -> tuple[int, int, int, str]:
    end = record.effective_end_ms if record.effective_end_ms is not None else 2**63 - 1
    return (
        record.generation,
        record.effective_start_ms,
        end,
        policy_content_fingerprint(record),
    )


def select_authority(records: list[PolicyRecord]) -> PolicyRecord:
    if not records:
        raise ValueError("cannot select authority from empty records")
    return sorted(records, key=lambda r: (_precedence(r), r.source.source_ref))[-1]


def _window_tuple(record: PolicyRecord) -> tuple[int, Optional[int]]:
    return (record.effective_start_ms, record.effective_end_ms)


def _conflict_fields(left: PolicyRecord, right: PolicyRecord) -> list[str]:
    fields: list[str] = []
    if left.action_code != right.action_code:
        fields.append("action_code")
    if left.match_digest != right.match_digest:
        fields.append("match_string")
    if _window_tuple(left) != _window_tuple(right):
        fields.append("effective_window")
    return fields


def policy_summary(record: PolicyRecord) -> dict[str, Any]:
    return {
        "policy_key": record.policy_key,
        "rule_generation": record.generation,
        "format": record.format,
        "action_code": record.action_code,
        "match_digest": record.match_digest,
        "effective_start_ms": record.effective_start_ms,
        "effective_end_ms": record.effective_end_ms,
        "tombstone": record.tombstone,
        "authority_source": record.source.to_dict(),
    }


def build_policy_view(records: list[PolicyRecord]) -> PolicyView:
    by_key: dict[str, list[PolicyRecord]] = {}
    for record in records:
        by_key.setdefault(record.policy_key, []).append(record)

    active: list[PolicyRecord] = []
    conflicts: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []

    for policy_key in sorted(by_key):
        key_records = by_key[policy_key]
        authority = select_authority(key_records)

        if authority.tombstone:
            covered = [
                item
                for item in key_records
                if not item.tombstone and _precedence(item) < _precedence(authority)
            ]
            if covered:
                tombstones.append(
                    {
                        "code": Codes.POLICY_TOMBSTONE,
                        "policy_key": policy_key,
                        "authoritative_tombstone": authority.basis_dict(),
                        "covered_records": [item.basis_dict() for item in sorted(covered, key=lambda r: r.source.source_ref)],
                    }
                )
            continue

        active.append(authority)

        by_generation: dict[int, list[PolicyRecord]] = {}
        for record in key_records:
            if not record.tombstone:
                by_generation.setdefault(record.generation, []).append(record)

        generations = sorted(by_generation)
        if 1 in by_generation and any(gen >= 2 for gen in generations):
            v1 = select_authority(by_generation[1])
            v2_generation = max(gen for gen in generations if gen >= 2)
            v2 = select_authority(by_generation[v2_generation])
            changed = _conflict_fields(v1, v2)
            if changed:
                winner = authority
                conflicts.append(
                    {
                        "code": Codes.POLICY_CONFLICT,
                        "policy_key": policy_key,
                        "conflict_fields": changed,
                        "generations": [v1.generation, v2.generation],
                        "left": v1.basis_dict(),
                        "right": v2.basis_dict(),
                        "authoritative_selection": {
                            "rule_generation": winner.generation,
                            "effective_start_ms": winner.effective_start_ms,
                            "source": winner.source.to_dict(),
                            "selection_order": [
                                "rule_generation_desc",
                                "effective_start_ms_desc",
                                "content_hash_desc",
                            ],
                        },
                    }
                )

    conflicts.sort(key=lambda item: item["policy_key"])
    tombstones.sort(key=lambda item: item["policy_key"])
    active.sort(key=lambda item: item.policy_key)
    return PolicyView(
        policies=active,
        conflicts=conflicts,
        tombstone_overrides=tombstones,
        records_by_key=by_key,
    )
