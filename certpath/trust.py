"""Merge v1/v2 trust anchors into one deterministic trust view."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from .canon import sha256_obj
from .errors import E_TRUST_CONFLICT
from .model import (
    Certificate,
    TrustAnchor,
    TrustView,
    V1AnchorRecord,
    V1Snapshot,
    V2AnchorRecord,
    V2Snapshot,
)


def _v1_effective(record: V1AnchorRecord) -> bool:
    return record.trusted and not record.revoked


def _v2_effective(record: V2AnchorRecord) -> bool:
    return record.trusted and not record.tombstone and not record.revoked


def _v2_order(record: V2AnchorRecord) -> tuple[int, int, int, str]:
    return (
        record.generation,
        record.effective_ms,
        1 if record.tombstone else 0,
        record.anchor_id,
    )


def _v1_order(record: V1AnchorRecord) -> tuple[int, int, str]:
    return (record.generation, record.effective_ms, record.certificate_id)


def _snapshot_digest(
    observation_clock_ms: int,
    certificates: list[Certificate],
    v1: V1Snapshot | None,
    v2: V2Snapshot | None,
) -> str:
    return sha256_obj(
        {
            "observation_clock_ms": observation_clock_ms,
            "certificates": [
                item.to_dict()
                for item in sorted(
                    certificates, key=lambda cert: cert.certificate_id
                )
            ],
            "v1": (
                {
                    "relative_effective_seconds": v1.relative_effective_seconds,
                    "trusted_root_ids": sorted(set(v1.trusted_root_ids)),
                    "revoked_root_ids": sorted(set(v1.revoked_root_ids)),
                    "no_path_length_constraint": v1.no_path_length_constraint,
                    "anchors": [
                        {
                            "certificate_id": item.certificate_id,
                            "fingerprint": item.fingerprint,
                            "subject_id": item.subject_id,
                            "trusted": item.trusted,
                            "revoked": item.revoked,
                        }
                        for item in sorted(
                            v1.anchors,
                            key=lambda anchor: (
                                anchor.certificate_id,
                                anchor.fingerprint,
                            ),
                        )
                    ],
                }
                if v1 is not None
                else None
            ),
            "v2": (
                {
                    "generation": v2.generation,
                    "effective_ms": v2.effective_ms,
                    "anchors": [
                        {
                            "anchor_id": item.anchor_id,
                            "fingerprint": item.fingerprint,
                            "subject_id": item.subject_id,
                            "effective_ms": item.effective_ms,
                            "generation": item.generation,
                            "path_length": item.path_length,
                            "trusted": item.trusted,
                            "tombstone": item.tombstone,
                            "revoked": item.revoked,
                        }
                        for item in sorted(
                            v2.anchors,
                            key=lambda anchor: (
                                anchor.fingerprint,
                                anchor.generation,
                                anchor.effective_ms,
                                anchor.anchor_id,
                            ),
                        )
                    ],
                }
                if v2 is not None
                else None
            ),
        }
    )


def _conflict(
    fingerprint: str,
    subject_id: str,
    field_name: str,
    v1_value: Any,
    v2_value: Any,
    v1_record: V1AnchorRecord,
    v2_record: V2AnchorRecord,
) -> dict[str, Any]:
    return {
        "code": E_TRUST_CONFLICT,
        "fingerprint": fingerprint,
        "subject_id": subject_id,
        "field": field_name,
        "v1": {
            "value": v1_value,
            "certificate_id": v1_record.certificate_id,
            "generation": v1_record.generation,
            "effective_ms": v1_record.effective_ms,
            "effective_interval": {
                "low_ms": v1_record.effective_low_ms,
                "high_ms": v1_record.effective_high_ms,
            },
        },
        "v2": {
            "value": v2_value,
            "anchor_id": v2_record.anchor_id,
            "generation": v2_record.generation,
            "effective_ms": v2_record.effective_ms,
        },
        "pending_confirmation": True,
    }


def _winner_v1(records: Iterable[V1AnchorRecord]) -> V1AnchorRecord:
    return max(records, key=_v1_order)


def _winner_v2(records: Iterable[V2AnchorRecord]) -> V2AnchorRecord:
    return max(records, key=_v2_order)


def build_trust_view(
    certificates: list[Certificate],
    v1: V1Snapshot | None,
    v2: V2Snapshot | None,
    observation_clock_ms: int,
) -> TrustView:
    """Merge present snapshots by canonical public-key fingerprint."""
    v1_groups: dict[str, list[V1AnchorRecord]] = defaultdict(list)
    v2_groups: dict[str, list[V2AnchorRecord]] = defaultdict(list)
    if v1 is not None:
        for record in v1.anchors:
            v1_groups[record.fingerprint].append(record)
    if v2 is not None:
        for record in v2.anchors:
            v2_groups[record.fingerprint].append(record)

    anchors: dict[str, TrustAnchor] = {}
    conflicts: list[dict[str, Any]] = []
    for fingerprint in sorted(set(v1_groups) | set(v2_groups)):
        v1_record = _winner_v1(v1_groups[fingerprint]) if fingerprint in v1_groups else None
        v2_record = _winner_v2(v2_groups[fingerprint]) if fingerprint in v2_groups else None
        chosen_conflicts: list[dict[str, Any]] = []

        candidates: list[tuple[tuple[Any, ...], str, Any]] = []
        if v1_record is not None:
            candidates.append(
                (
                    (
                        v1_record.generation,
                        v1_record.effective_ms,
                        0,
                        v1_record.certificate_id,
                    ),
                    "v1",
                    v1_record,
                )
            )
        if v2_record is not None:
            candidates.append(
                (
                    (
                        v2_record.generation,
                        v2_record.effective_ms,
                        1,
                        v2_record.anchor_id,
                    ),
                    "v2",
                    v2_record,
                )
            )
        _, source, record = max(candidates, key=lambda item: item[0])

        subject_id = record.subject_id
        if v1_record is not None and v2_record is not None:
            v1_trusted = _v1_effective(v1_record)
            v2_trusted = _v2_effective(v2_record)
            if v1_record.subject_id != v2_record.subject_id:
                chosen_conflicts.append(
                    _conflict(
                        fingerprint,
                        subject_id,
                        "subject_id",
                        v1_record.subject_id,
                        v2_record.subject_id,
                        v1_record,
                        v2_record,
                    )
                )
            if v1_trusted != v2_trusted:
                chosen_conflicts.append(
                    _conflict(
                        fingerprint,
                        subject_id,
                        "trusted",
                        v1_trusted,
                        v2_trusted,
                        v1_record,
                        v2_record,
                    )
                )
            if v1_record.revoked != v2_record.revoked:
                chosen_conflicts.append(
                    _conflict(
                        fingerprint,
                        subject_id,
                        "revoked",
                        v1_record.revoked,
                        v2_record.revoked,
                        v1_record,
                        v2_record,
                    )
                )
            if v1_record.path_length != v2_record.path_length:
                chosen_conflicts.append(
                    _conflict(
                        fingerprint,
                        subject_id,
                        "path_length",
                        v1_record.path_length,
                        v2_record.path_length,
                        v1_record,
                        v2_record,
                    )
                )

        if source == "v1":
            assert v1_record is not None
            anchor = TrustAnchor(
                fingerprint=fingerprint,
                subject_id=v1_record.subject_id,
                trusted=_v1_effective(v1_record),
                path_length=None,
                generation=v1_record.generation,
                effective_ms=v1_record.effective_ms,
                effective_low_ms=v1_record.effective_low_ms,
                effective_high_ms=v1_record.effective_high_ms,
                source="v1",
                source_record_ids=[v1_record.certificate_id],
                tombstone=False,
                revoked=v1_record.revoked,
            )
        else:
            assert v2_record is not None
            anchor = TrustAnchor(
                fingerprint=fingerprint,
                subject_id=v2_record.subject_id,
                trusted=_v2_effective(v2_record),
                path_length=v2_record.path_length,
                generation=v2_record.generation,
                effective_ms=v2_record.effective_ms,
                effective_low_ms=v2_record.effective_ms,
                effective_high_ms=v2_record.effective_ms,
                source="v2",
                source_record_ids=[v2_record.anchor_id],
                tombstone=v2_record.tombstone,
                revoked=v2_record.revoked,
            )
        anchor.conflicts = chosen_conflicts
        anchor.pending_confirmation = bool(chosen_conflicts)
        anchors[fingerprint] = anchor
        conflicts.extend(chosen_conflicts)

    conflicts.sort(
        key=lambda item: (
            item["fingerprint"],
            item["field"],
            item["v1"]["generation"],
            item["v2"]["generation"],
        )
    )
    generation = max((item.generation for item in anchors.values()), default=0)
    digest = _snapshot_digest(observation_clock_ms, certificates, v1, v2)
    return TrustView(
        anchors=anchors,
        conflicts=conflicts,
        snapshot_digest=digest,
        generation=generation,
        observation_clock_ms=observation_clock_ms,
        v1_quantization=v1.conversion if v1 is not None else None,
    )
