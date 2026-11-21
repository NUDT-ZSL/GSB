from __future__ import annotations

from typing import Any

from .config import STAGE_ORDER
from .errors import fail
from .features import (
    CallMeta,
    validate_feature_record,
)
from .lineage import check_partition, replay_event
from .noise import validate_noise_record
from .profile import validate_profile_record
from .store import Snapshot, SnapshotStore
from .attribution import validate_attribution_record


def _snapshot_or_fail(store: SnapshotStore, stage: str) -> Snapshot:
    snapshot = store.get_current(stage)
    if snapshot is None:
        fail("E_INCONSISTENT", stage=stage, detail="required snapshot is missing")
    return snapshot


def run_consistency(
    store: SnapshotStore,
    calls: list[CallMeta],
) -> dict[str, Any]:
    stages = {
        stage: _snapshot_or_fail(store, stage) for stage in STAGE_ORDER
    }
    manifest_ids = {call.call_id for call in calls}
    for stage, snapshot in stages.items():
        call_ids = set(snapshot.completed_call_ids)
        record_ids = {str(record["call_id"]) for record in snapshot.records}
        if call_ids != manifest_ids or record_ids != manifest_ids:
            fail(
                "E_INCONSISTENT",
                stage=stage,
                detail="call_id sets differ across stages",
                snapshot_calls=sorted(call_ids),
                record_calls=sorted(record_ids),
                manifest_calls=sorted(manifest_ids),
            )
        invalidation = snapshot.invalidation
        check_partition(
            list(invalidation.get("recomputed_calls", [])),
            list(invalidation.get("reused_calls", [])),
            list(invalidation.get("affected_calls", [])),
        )
        coverage = set(invalidation.get("recomputed_calls", [])) | set(
            invalidation.get("reused_calls", [])
        )
        if coverage and coverage != call_ids:
            fail(
                "E_INCONSISTENT",
                stage=stage,
                detail="reuse/recompute coverage differs from snapshot calls",
            )

    feature_records = stages["features"].record_map()
    noise_records = stages["noise_floor"].record_map()
    attr_records = stages["attribution"].record_map()
    profile_records = stages["profile"].record_map()
    for call in calls:
        feature_record = feature_records[call.call_id]
        noise_record = noise_records[call.call_id]
        attr_record = attr_records[call.call_id]
        profile_record = profile_records[call.call_id]
        validate_feature_record(feature_record)
        validate_noise_record(noise_record)
        validate_attribution_record(attr_record)
        if noise_record.get("source_feature_checksum") != feature_record.get(
            "record_checksum"
        ):
            fail(
                "E_INCONSISTENT",
                stage="noise_floor",
                call_id=call.call_id,
                detail="feature checksum chain mismatch",
            )
        if attr_record.get("source_noise_checksum") != noise_record.get(
            "record_checksum"
        ):
            fail(
                "E_INCONSISTENT",
                stage="attribution",
                call_id=call.call_id,
                detail="noise checksum chain mismatch",
            )
        if attr_record.get("noise_steps") != noise_record.get("steps"):
            fail(
                "E_INCONSISTENT",
                stage="attribution",
                call_id=call.call_id,
                detail="noise step chain mismatch",
            )
        if profile_record.get(
            "source_attribution_checksum"
        ) != attr_record.get("record_checksum"):
            fail(
                "E_INCONSISTENT",
                stage="profile",
                call_id=call.call_id,
                detail="attribution checksum chain mismatch",
            )
        if profile_record.get("noise_steps") != attr_record.get("noise_steps"):
            fail(
                "E_INCONSISTENT",
                stage="profile",
                call_id=call.call_id,
                detail="profile noise step chain mismatch",
            )
        profile_config = stages["profile"].stage_config
        try:
            validate_profile_record(
                call,
                attr_record,
                profile_record,
                profile_config,
                list(profile_record.get("controlled_labels", [])),
            )
        except Exception as exc:
            from .errors import BusinessError

            if isinstance(exc, BusinessError):
                raise
            fail(
                "E_INCONSISTENT",
                stage="profile",
                call_id=call.call_id,
                detail=f"profile validation failed: {exc}",
            )
        for event in profile_record.get("events", []):
            replay_event(store, event, call)

    return {
        "code": "OK",
        "stages": {
            stage: {
                "version": snapshot.version,
                "checksum": snapshot.checksum,
                "completed_call_ids": snapshot.completed_call_ids,
            }
            for stage, snapshot in stages.items()
        },
        "call_set_equal": True,
        "partition_valid": True,
        "profile_recomputed": True,
        "lineage_replayed_events": sum(
            len(record.get("events", [])) for record in profile_records.values()
        ),
    }

