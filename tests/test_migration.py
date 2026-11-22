from __future__ import annotations

from pathlib import Path

from flightfusion.formats import parse_description_file
from flightfusion.formats import parse_document
from flightfusion.migration import behavior_fingerprint
from flightfusion.migration import (
    downgrade_v2_to_v1,
    downgrade_v3_to_v2,
    migrate_descriptions,
    upgrade_v1_to_v2,
    upgrade_v2_to_v3,
    verify_behavior_preserved,
)


def _formats(workspace_dir: Path):
    base = workspace_dir / "data" / "formats"
    return {
        1: parse_description_file(base / "sensors.v1.txt")[1],
        2: parse_description_file(base / "sensors.v2.csv")[1],
        3: parse_description_file(base / "sensors.v3.json")[1],
    }


def test_upgrade_completions_are_itemized(workspace_dir):
    formats = _formats(workspace_dir)
    v1_to_v2 = upgrade_v1_to_v2(formats[1])
    assert verify_behavior_preserved(formats[1], v1_to_v2.descriptions)
    assert len(v1_to_v2.completions) == len(formats[1]) * 7
    assert all(item["source"] for item in v1_to_v2.completions)

    v2_to_v3 = upgrade_v2_to_v3(formats[2])
    assert verify_behavior_preserved(formats[2], v2_to_v3.descriptions)
    fields = {(item["sensor_id"], item["field"]) for item in v2_to_v3.completions}
    assert ("gyro", "axis_coupling.xx") in fields
    assert ("accel", "noise_model.white_noise_std") in fields
    assert ("mag", "temperature_compensation.reference_c") in fields
    assert all(
        item["source"].startswith(("defaults.v3", "v2."))
        for item in v2_to_v3.completions
    )


def test_downgrade_v3_to_v2_flattens_without_behavior_change(workspace_dir):
    formats = _formats(workspace_dir)
    result = downgrade_v3_to_v2(formats[3])
    assert verify_behavior_preserved(formats[3], result.descriptions)
    assert any(item["action"] == "flatten" for item in result.transformations)
    assert not result.losses
    assert "coupling_xx" in result.serialized
    assert "noise_white" in result.serialized


def test_downgrade_v2_to_v1_reports_diagonal_and_drop_policy(workspace_dir):
    formats = _formats(workspace_dir)
    flattened = downgrade_v3_to_v2(formats[3])
    result = downgrade_v2_to_v1(flattened.descriptions)
    assert verify_behavior_preserved(flattened.descriptions, result.descriptions)
    coupling = [item for item in result.losses if item["field"] == "axis_coupling"]
    noise = [item for item in result.losses if item["field"] == "noise_model"]
    assert coupling and all(item["action"] == "diagonal_approximation" for item in coupling)
    assert noise and all(item["action"] == "drop" for item in noise)

    dropped = downgrade_v2_to_v1(
        flattened.descriptions,
        policy={"axis_coupling": "drop"},
    )
    drop_entries = [
        item for item in dropped.losses if item["field"] == "axis_coupling"
    ]
    assert drop_entries and all(item["action"] == "drop" for item in drop_entries)


def test_multi_hop_migration_chain(workspace_dir):
    formats = _formats(workspace_dir)
    upgraded = migrate_descriptions(formats[1], 1, 3)
    downgraded = migrate_descriptions(formats[3], 3, 1)
    assert upgraded.target_version == 3
    assert downgraded.target_version == 1
    assert verify_behavior_preserved(formats[1], upgraded.descriptions)
    assert verify_behavior_preserved(formats[3], downgraded.descriptions)


def test_migrated_serialized_documents_replay_behavior(workspace_dir):
    formats = _formats(workspace_dir)
    results = [
        upgrade_v1_to_v2(formats[1]),
        upgrade_v2_to_v3(formats[2]),
        downgrade_v3_to_v2(formats[3]),
    ]
    for result in results:
        _version, reparsed = parse_document(result.serialized)
        for sensor_id, migrated in result.descriptions.items():
            assert behavior_fingerprint(migrated) == behavior_fingerprint(
                reparsed[sensor_id]
            )
    flattened = downgrade_v3_to_v2(formats[3])
    downgraded = downgrade_v2_to_v1(flattened.descriptions)
    _version, reparsed_v1 = parse_document(downgraded.serialized)
    for sensor_id, migrated in downgraded.descriptions.items():
        assert behavior_fingerprint(migrated) == behavior_fingerprint(
            reparsed_v1[sensor_id]
        )

