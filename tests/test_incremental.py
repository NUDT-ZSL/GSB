from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import (
    cli_json,
    decision_rows,
    ledger_rows,
    run_cli,
    samples_dir,
)


def _baseline(workspace: Path, samples: Path) -> None:
    assert cli_json(
        "import-events",
        "--workspace",
        workspace,
        "--events",
        samples / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        samples / "rules_snapshot.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", workspace)["exit_code"] == 0


def test_incremental_patch_order_matches_full_and_is_subset(tmp_path, samples_dir):
    left = tmp_path / "left"
    right = tmp_path / "right"
    _baseline(left, samples_dir)
    _baseline(right, samples_dir)

    left_import = cli_json(
        "import-rules",
        "--workspace",
        left,
        "--patch",
        samples_dir / "patch-003-address.json",
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
    )
    right_import = cli_json(
        "import-rules",
        "--workspace",
        right,
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
        "--patch",
        samples_dir / "patch-003-address.json",
    )
    assert left_import["patches_imported"] == 3
    assert right_import["patches_imported"] == 3

    left_recompute = cli_json("recompute", "--workspace", left)
    right_recompute = cli_json("recompute", "--workspace", right)
    assert left_recompute["exit_code"] == 0
    assert right_recompute["exit_code"] == 0
    assert left_recompute["impacted_event_count"] < 12
    assert left_recompute["impacted_event_count"] == right_recompute["impacted_event_count"]

    impacted_ids = {
        item["event_id"] for item in left_recompute["impacted_events"]
    }
    assert not impacted_ids.intersection(
        {"evt-002", "evt-003", "evt-006", "evt-009", "evt-011"}
    )
    assert decision_rows(left) == decision_rows(right)
    phone_decisions = [
        row
        for row in ledger_rows(left)
        if row["event_id"] == "evt-000" and row["field_path"] == "payload.phone"
    ]
    assert phone_decisions[0]["winning_rule_name"] == "contact-phone"
    assert phone_decisions[0]["winning_generation"] == 3
    assert phone_decisions[0]["winning_threshold"] == 70

    consistency = cli_json("verify-full", "--workspace", left)
    assert consistency["mismatch_count"] == 0
    assert consistency["exit_code"] == 0

    full = tmp_path / "full"
    assert cli_json(
        "import-events",
        "--workspace",
        full,
        "--events",
        samples_dir / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        full,
        "--snapshot",
        samples_dir / "rules_snapshot.json",
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
        "--patch",
        samples_dir / "patch-003-address.json",
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", full)["exit_code"] == 0
    assert decision_rows(full) == decision_rows(left)


def test_event_arrival_order_does_not_change_decisions(tmp_path, samples_dir):
    forward = tmp_path / "forward"
    reverse = tmp_path / "reverse"
    for workspace in (forward, reverse):
        assert cli_json(
            "import-rules",
            "--workspace",
            workspace,
            "--snapshot",
            samples_dir / "rules_snapshot.json",
        )["exit_code"] == 0
    assert cli_json(
        "import-events",
        "--workspace",
        forward,
        "--events",
        samples_dir / "events_batch_1.jsonl",
        samples_dir / "events_batch_2.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-events",
        "--workspace",
        reverse,
        "--events",
        samples_dir / "events_batch_2.jsonl",
        samples_dir / "events_batch_1.jsonl",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", forward)["exit_code"] == 0
    assert cli_json("recompute", "--workspace", reverse)["exit_code"] == 0
    assert decision_rows(forward) == decision_rows(reverse)


def test_patch_reimport_is_idempotent(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    _baseline(workspace, samples_dir)
    first = cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
    )
    second = cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
    )
    assert first["patches_imported"] == 1
    assert second["patches_imported"] == 0
    assert second["patches_skipped_idempotent"] == [
        "patch-001-phone-upgrade"
    ]


def test_bad_snapshot_does_not_block_valid_patch_or_replace_rules(
    tmp_path,
    samples_dir,
):
    workspace = tmp_path / "ws"
    _baseline(workspace, samples_dir)
    bad_snapshot = tmp_path / "bad_rules.txt"
    bad_snapshot.write_text("not a snapshot\n", encoding="utf-8")
    imported = cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        bad_snapshot,
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
    )
    assert imported["exit_code"] == 1
    assert imported["snapshot_rejected"] is True
    assert imported["patches_imported"] == 1
    assert cli_json("recompute", "--workspace", workspace)["exit_code"] == 0
    assert cli_json("verify-full", "--workspace", workspace)["mismatch_count"] == 0
    decisions = ledger_rows(workspace)
    contact_email = next(
        row
        for row in decisions
        if row["event_id"] == "evt-000"
        and row["field_path"] == "payload.email"
    )
    assert contact_email["winning_rule_name"] == "contact-email"


def test_recompute_after_each_out_of_order_patch_converges(tmp_path, samples_dir):
    workspace = tmp_path / "sequential"
    _baseline(workspace, samples_dir)
    for patch_name in (
        "patch-002-auth-tombstone.json",
        "patch-003-address.json",
        "patch-001-phone-upgrade.json",
    ):
        assert cli_json(
            "import-rules",
            "--workspace",
            workspace,
            "--patch",
            samples_dir / patch_name,
        )["exit_code"] == 0
        assert cli_json("recompute", "--workspace", workspace)["exit_code"] == 0
        assert cli_json("verify-full", "--workspace", workspace)["exit_code"] == 0

    fresh = tmp_path / "fresh"
    assert cli_json(
        "import-events",
        "--workspace",
        fresh,
        "--events",
        samples_dir / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        fresh,
        "--snapshot",
        samples_dir / "rules_snapshot.json",
        "--patch",
        samples_dir / "patch-001-phone-upgrade.json",
        "--patch",
        samples_dir / "patch-002-auth-tombstone.json",
        "--patch",
        samples_dir / "patch-003-address.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", fresh)["exit_code"] == 0
    assert decision_rows(workspace) == decision_rows(fresh)


def test_layout_fingerprint_change_rebuilds_ledger(tmp_path, samples_dir):
    workspace = tmp_path / "ws"
    _baseline(workspace, samples_dir)
    old_rows = ledger_rows(workspace)
    old_fingerprint = old_rows[0]["layout_fingerprint"]
    checkpoint = cli_json(
        "checkpoint",
        "create",
        "--workspace",
        workspace,
    )
    assert checkpoint["exit_code"] == 0

    imported = cli_json(
        "import-rules",
        "--workspace",
        workspace,
        "--snapshot",
        samples_dir / "rules_snapshot_layout_v2.json",
    )
    assert imported["exit_code"] == 0
    recomputed = cli_json("recompute", "--workspace", workspace)
    assert recomputed["layout_rebuilt"] is True
    assert recomputed["impacted_event_count"] == 12
    assert recomputed["business_codes"]["I_LAYOUT_REBUILD"] == 1
    new_rows = ledger_rows(workspace)
    assert new_rows
    assert {row["layout_fingerprint"] for row in new_rows} != {old_fingerprint}
    assert len({row["layout_fingerprint"] for row in new_rows}) == 1

    connection = sqlite3.connect(workspace / "audit_masker.sqlite3")
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints"
        ).fetchone()[0]
    finally:
        connection.close()
    assert count == 0

    reimported = tmp_path / "reimported"
    assert cli_json(
        "import-events",
        "--workspace",
        reimported,
        "--events",
        samples_dir / "events.jsonl",
    )["exit_code"] == 0
    assert cli_json(
        "import-rules",
        "--workspace",
        reimported,
        "--snapshot",
        samples_dir / "rules_snapshot_layout_v2.json",
    )["exit_code"] == 0
    assert cli_json("recompute", "--workspace", reimported)["exit_code"] == 0
    assert decision_rows(workspace) == decision_rows(reimported)
