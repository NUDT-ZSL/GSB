from __future__ import annotations


def test_revocation_downgrade_and_alternative(imported_store):
    report = imported_store.export_reentry_report()
    assert "obj:revoke-down" in report["changed_object_set"]
    assert "obj:revoke-retain" in report["reentry_set"]
    assert "obj:revoke-retain" not in report["changed_object_set"]

    downgrade = next(
        event
        for event in report["events"]
        if event["trigger_type"] == "revoke_source"
        and event["object_key"] == "obj:revoke-down"
    )
    assert downgrade["before_level"] == "RESTRICTED"
    assert downgrade["after_level"] == "CONFIDENTIAL"
    assert downgrade["changed"] is True

    retain = next(
        event
        for event in report["events"]
        if event["trigger_type"] == "revoke_source"
        and event["object_key"] == "obj:revoke-retain"
    )
    assert retain["before_level"] == "RESTRICTED"
    assert retain["after_level"] == "RESTRICTED"
    assert retain["decision"] == "RETAINED_WITH_ALTERNATIVE"
    alt_keys = {item["source_key"] for item in retain["alternative_sources"]}
    assert "src:retain-alt-bob-restricted" in alt_keys


def test_migration_reentry_set_and_path_markers(imported_store):
    report = imported_store.export_reentry_report()
    assert report["migration_changed_set"] == [
        "obj:mig-gain",
        "obj:mig-loss",
        "obj:mig-rebalance",
    ]
    gain = next(
        event
        for event in report["events"]
        if event["trigger_type"] == "migrate_trustee" and event["object_key"] == "obj:mig-gain"
    )
    assert gain["before_level"] is None
    assert gain["after_level"] == "CONFIDENTIAL"
    assert gain["new_path_marker"].startswith("gpm_")

    loss = next(
        event
        for event in report["events"]
        if event["trigger_type"] == "migrate_trustee" and event["object_key"] == "obj:mig-loss"
    )
    assert loss["before_level"] == "RESTRICTED"
    assert loss["after_level"] is None

    evaluated = {
        event["object_key"]
        for event in report["events"]
        if event["trigger_type"] == "migrate_trustee"
    }
    # The whole new-path closure is evaluated even when the level does not change.
    assert "obj:finance" in evaluated
    assert "obj:finance" not in report["migration_changed_set"]
