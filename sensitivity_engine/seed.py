"""Deterministic fixed-seed sample generator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .atomicio import atomic_write_json
from .domain import LAYOUT_VERSION, LEVEL_ENUM_VERSION
from .hashing import marker


SEED_ID = "fixed-sensitive-seed-20261125"
FIXED_SEED = 20261125


def grant_marker(source_key: str, level: str) -> str:
    return marker("grm", source_key, level)


def path_marker(path: list[str]) -> str:
    return marker("gpm", ">".join(path))


def revocation_marker(source_key: str, op_seq: int) -> str:
    return marker("rvm", "batch:rb-002-revoke", op_seq, source_key)


def _source(
    seq: int,
    source_key: str,
    object_key: str,
    trustee_id: str,
    level: str,
    *,
    valid_from: int = 0,
    valid_to: int | None = 1000,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "source_key": source_key,
        "object_key": object_key,
        "trustee_id": trustee_id,
        "level": level,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "grant_marker": grant_marker(source_key, level),
    }


def build_main_sample() -> tuple[dict[str, Any], dict[str, Any]]:
    metadata = {
        "seed_id": SEED_ID,
        "fixed_seed": FIXED_SEED,
        "layout_version": LAYOUT_VERSION,
        "level_enum_version": LEVEL_ENUM_VERSION,
        "evaluation_clock": 20,
    }

    objects = [
        {"seq": 10, "object_key": "obj:root", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 20, "object_key": "obj:finance", "parent_key": "obj:root", "inheritance_mode": "ALLOW"},
        {"seq": 30, "object_key": "obj:finance-audit", "parent_key": "obj:finance", "inheritance_mode": "ALLOW"},
        {"seq": 40, "object_key": "obj:hr", "parent_key": "obj:root", "inheritance_mode": "ALLOW"},
        {"seq": 50, "object_key": "obj:hr-secret", "parent_key": "obj:hr", "inheritance_mode": "BLOCK"},
        {"seq": 60, "object_key": "obj:revoke-down", "parent_key": "obj:root", "inheritance_mode": "ALLOW"},
        {"seq": 70, "object_key": "obj:revoke-retain", "parent_key": "obj:root", "inheritance_mode": "ALLOW"},
        {"seq": 80, "object_key": "obj:mig-gain", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 90, "object_key": "obj:mig-loss", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 100, "object_key": "obj:mig-rebalance", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 110, "object_key": "obj:batch-add", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 120, "object_key": "obj:empty", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 130, "object_key": "obj:window", "parent_key": None, "inheritance_mode": "ALLOW"},
        {"seq": 140, "object_key": "obj:cycle-target", "parent_key": None, "inheritance_mode": "ALLOW"},
    ]

    trustees = [
        {"trustee_id": "trustee:alice", "kind": "USER", "display_name": "Alice"},
        {"trustee_id": "trustee:bob", "kind": "USER", "display_name": "Bob"},
        {"trustee_id": "trustee:carol", "kind": "USER", "display_name": "Carol"},
        {"trustee_id": "trustee:dave", "kind": "USER", "display_name": "Dave"},
        {"trustee_id": "group:staff", "kind": "GROUP", "display_name": "Staff"},
        {"trustee_id": "group:compliance", "kind": "GROUP", "display_name": "Compliance"},
        {"trustee_id": "group:finance-group", "kind": "GROUP", "display_name": "Finance Group"},
        {"trustee_id": "group:audit-desk", "kind": "GROUP", "display_name": "Audit Desk"},
        {"trustee_id": "group:legacy", "kind": "GROUP", "display_name": "Legacy"},
        {"trustee_id": "group:cycle-a", "kind": "GROUP"},
        {"trustee_id": "group:cycle-b", "kind": "GROUP"},
        {"trustee_id": "group:self-loop", "kind": "GROUP"},
    ]

    group_edges = [
        {"parent_id": "group:staff", "child_id": "group:compliance", "seq": 10},
        {"parent_id": "group:compliance", "child_id": "trustee:alice", "seq": 20},
        {"parent_id": "group:compliance", "child_id": "trustee:bob", "seq": 30},
        {"parent_id": "group:compliance", "child_id": "group:audit-desk", "seq": 40},
        {"parent_id": "group:finance-group", "child_id": "trustee:carol", "seq": 50},
        {"parent_id": "group:legacy", "child_id": "trustee:dave", "seq": 60},
        {"parent_id": "group:cycle-a", "child_id": "group:cycle-b", "seq": 70},
        {"parent_id": "group:cycle-b", "child_id": "group:cycle-a", "seq": 80},
        {"parent_id": "group:self-loop", "child_id": "group:self-loop", "seq": 90},
    ]

    sources = [
        _source(10, "src:root-staff-internal", "obj:root", "group:staff", "INTERNAL"),
        _source(20, "src:finance-compliance-restricted", "obj:finance", "group:compliance", "RESTRICTED"),
        _source(30, "src:finance-alice-confidential", "obj:finance", "trustee:alice", "CONFIDENTIAL"),
        _source(40, "src:hr-finance-confidential", "obj:hr", "group:finance-group", "CONFIDENTIAL"),
        _source(50, "src:hr-secret-alice-restricted", "obj:hr-secret", "trustee:alice", "RESTRICTED"),
        _source(60, "src:down-high-alice-restricted", "obj:revoke-down", "trustee:alice", "RESTRICTED"),
        _source(70, "src:down-low-bob-confidential", "obj:revoke-down", "trustee:bob", "CONFIDENTIAL"),
        _source(80, "src:retain-primary-carol-restricted", "obj:revoke-retain", "trustee:carol", "RESTRICTED"),
        _source(90, "src:retain-alt-bob-restricted", "obj:revoke-retain", "trustee:bob", "RESTRICTED"),
        _source(100, "src:gain-audit-confidential", "obj:mig-gain", "group:audit-desk", "CONFIDENTIAL"),
        _source(110, "src:loss-legacy-restricted", "obj:mig-loss", "group:legacy", "RESTRICTED"),
        _source(120, "src:rebalance-old-legacy-restricted", "obj:mig-rebalance", "group:legacy", "RESTRICTED"),
        _source(130, "src:rebalance-new-audit-internal", "obj:mig-rebalance", "group:audit-desk", "INTERNAL"),
        _source(140, "src:window-active-internal", "obj:window", "trustee:alice", "INTERNAL", valid_from=0, valid_to=25),
        _source(150, "src:window-expired-restricted", "obj:window", "trustee:bob", "RESTRICTED", valid_from=0, valid_to=5),
        _source(160, "src:window-future-restricted", "obj:window", "trustee:carol", "RESTRICTED", valid_from=30, valid_to=100),
        _source(170, "src:cycle-grant-restricted", "obj:cycle-target", "group:cycle-a", "RESTRICTED"),
    ]

    add_op = {
        "op_seq": 10,
        "op_type": "add_source",
        "source_key": "src:batch-added-carol-confidential",
        "object_key": "obj:batch-add",
        "trustee_id": "trustee:carol",
        "level": "CONFIDENTIAL",
        "valid_from": 0,
        "valid_to": 1000,
        "grant_marker": grant_marker("src:batch-added-carol-confidential", "CONFIDENTIAL"),
    }
    batch_add = {
        "batch_id": "batch:rb-001-add",
        "logical_clock": 10,
        "operations": [add_op],
    }

    revoke_down_marker = revocation_marker("src:down-high-alice-restricted", 10)
    revoke_retain_marker = revocation_marker("src:retain-primary-carol-restricted", 20)
    batch_revoke = {
        "batch_id": "batch:rb-002-revoke",
        "logical_clock": 20,
        "operations": [
            {
                "op_seq": 10,
                "op_type": "revoke_source",
                "source_key": "src:down-high-alice-restricted",
                "reason": "OWNER_REVOKED",
                "revocation_marker": revoke_down_marker,
            },
            {
                "op_seq": 20,
                "op_type": "revoke_source",
                "source_key": "src:retain-primary-carol-restricted",
                "reason": "OWNER_REVOKED",
                "revocation_marker": revoke_retain_marker,
            },
        ],
    }

    old_path = ["group:legacy", "trustee:dave"]
    new_path = ["group:audit-desk", "trustee:dave"]
    new_nested_path = ["group:staff", "group:compliance", "group:audit-desk", "trustee:dave"]
    batch_migrate = {
        "batch_id": "batch:rb-003-migrate",
        "logical_clock": 30,
        "operations": [
            {
                "op_seq": 10,
                "op_type": "migrate_trustee",
                "trustee_id": "trustee:dave",
                "old_parent_id": "group:legacy",
                "new_parent_id": "group:audit-desk",
                "old_path_marker": path_marker(old_path),
                "new_path_marker": path_marker(new_path),
            }
        ],
    }

    files = {
        "metadata.json": metadata,
        "objects.json": objects,
        "trustees.json": trustees,
        "group_edges.json": group_edges,
        "sources.json": sources,
        "batches/batch-001-add.json": batch_add,
        "batches/batch-002-revoke.json": batch_revoke,
        "batches/batch-003-migrate.json": batch_migrate,
    }

    expected_levels = {
        "obj:root": "INTERNAL",
        "obj:finance": "RESTRICTED",
        "obj:finance-audit": "RESTRICTED",
        "obj:hr": "CONFIDENTIAL",
        "obj:hr-secret": "RESTRICTED",
        "obj:revoke-down": "CONFIDENTIAL",
        "obj:revoke-retain": "RESTRICTED",
        "obj:mig-gain": "CONFIDENTIAL",
        "obj:mig-loss": None,
        "obj:mig-rebalance": "INTERNAL",
        "obj:batch-add": "CONFIDENTIAL",
        "obj:empty": None,
        "obj:window": "INTERNAL",
        "obj:cycle-target": None,
    }
    expected_status = {
        "obj:mig-loss": "NO_BASIS",
        "obj:empty": "NO_BASIS",
        "obj:cycle-target": "UNDECIDABLE",
    }
    expected_changed = [
        "obj:batch-add",
        "obj:mig-gain",
        "obj:mig-loss",
        "obj:mig-rebalance",
        "obj:revoke-down",
    ]
    expected_reentry = sorted(expected_changed + ["obj:revoke-retain"])

    truth = {
        "truth_type": "sensitivity-fixed-seed-truth",
        **metadata,
        "raw_grants": sources + [add_op],
        "revocations": [
            {
                "source_key": "src:down-high-alice-restricted",
                "op_seq": 10,
                "batch_id": "batch:rb-002-revoke",
                "revocation_marker": revoke_down_marker,
            },
            {
                "source_key": "src:retain-primary-carol-restricted",
                "op_seq": 20,
                "batch_id": "batch:rb-002-revoke",
                "revocation_marker": revoke_retain_marker,
            },
        ],
        "group_paths": [
            {
                "path_id": "dave_old",
                "phase": "old",
                "trustee_id": "trustee:dave",
                "path": old_path,
                "path_marker": path_marker(old_path),
            },
            {
                "path_id": "dave_new_direct",
                "phase": "new",
                "trustee_id": "trustee:dave",
                "path": new_path,
                "path_marker": path_marker(new_path),
            },
            {
                "path_id": "dave_new_nested",
                "phase": "new",
                "trustee_id": "trustee:dave",
                "path": new_nested_path,
                "path_marker": path_marker(new_nested_path),
            },
            {
                "path_id": "finance_alice",
                "phase": "current",
                "trustee_id": "trustee:alice",
                "path": ["group:compliance", "trustee:alice"],
                "path_marker": path_marker(["group:compliance", "trustee:alice"]),
            },
        ],
        "expected_effective_levels": expected_levels,
        "expected_decision_status": expected_status,
        "expected_reentry_set": expected_reentry,
        "expected_changed_object_set": sorted(expected_changed),
        "expected_migration_changed_set": [
            "obj:mig-gain",
            "obj:mig-loss",
            "obj:mig-rebalance",
        ],
        "expected_alternatives": {
            "obj:revoke-retain": [
                {
                    "revoked_source_key": "src:retain-primary-carol-restricted",
                    "alternative_source_key": "src:retain-alt-bob-restricted",
                }
            ]
        },
        "expected_group_cycles": [
            ["group:cycle-a", "group:cycle-b"],
            ["group:self-loop"],
        ],
        "sync_marker_index": {
            "grant_markers": [item["grant_marker"] for item in sources + [add_op]],
            "path_markers": [path_marker(item["path"]) for item in [
                {"path": old_path},
                {"path": new_path},
                {"path": new_nested_path},
                {"path": ["group:compliance", "trustee:alice"]},
            ]],
            "revocation_markers": [revoke_down_marker, revoke_retain_marker],
        },
    }
    return files, truth


def build_invalid_sample() -> tuple[dict[str, Any], dict[str, Any]]:
    files = {
        "metadata.json": {
            "seed_id": "invalid-records-seed",
            "layout_version": LAYOUT_VERSION,
            "level_enum_version": LEVEL_ENUM_VERSION,
            "evaluation_clock": 1,
        },
        "objects.json": [
            {"seq": 1, "object_key": "obj:ok", "parent_key": None, "inheritance_mode": "ALLOW"},
            {"seq": 2, "object_key": "obj:dup", "parent_key": None, "inheritance_mode": "ALLOW"},
            {"seq": 3, "object_key": "obj:dup", "parent_key": None, "inheritance_mode": "ALLOW"},
            {"seq": 4, "object_key": "obj:bad-parent", "parent_key": "obj:missing", "inheritance_mode": "ALLOW"},
            {"seq": 5, "object_key": "obj:self-parent", "parent_key": "obj:self-parent", "inheritance_mode": "ALLOW"},
            {"seq": 6, "object_key": "obj:bad-inherit", "parent_key": None, "inheritance_mode": "MAYBE"},
        ],
        "trustees.json": [
            {"trustee_id": "trustee:alice", "kind": "USER"},
        ],
        "sources.json": [
            {
                "seq": 1,
                "source_key": "src:bad-level",
                "object_key": "obj:ok",
                "trustee_id": "trustee:alice",
                "level": "ULTRA",
            },
            {
                "seq": 2,
                "source_key": "src:ok",
                "object_key": "obj:ok",
                "trustee_id": "trustee:alice",
                "level": "INTERNAL",
            },
        ],
        "batches/bad-sequence.json": {
            "batch_id": "batch:invalid-seq",
            "logical_clock": 1,
            "operations": [
                {"op_seq": 1, "op_type": "revoke_source", "source_key": "src:ok"},
                {"op_seq": 2, "op_type": "revoke_source", "source_key": "src:missing-syntactically-valid"},
                {"op_seq": 1, "op_type": "revoke_source", "source_key": "src:seq-rollback"},
            ],
        },
    }
    truth = {
        "truth_type": "sensitivity-invalid-truth",
        "expected_rejection_codes": [
            "E_DUPLICATE_OBJECT_KEY",
            "E_PARENT_NOT_FOUND",
            "E_SELF_PARENT",
            "E_INVALID_INHERITANCE_MARK",
            "E_INVALID_LEVEL",
            "E_SEQ_NOT_MONOTONIC",
        ],
    }
    return files, truth


def write_sample_workspace(
    root: str | Path = ".",
    *,
    data_dir: str | Path = "data",
    truth_file: str | Path = "truth/expected.json",
) -> dict[str, Any]:
    root_path = Path(root)
    data_path = root_path / data_dir
    main_files, truth = build_main_sample()
    for relative, payload in main_files.items():
        target = data_path / relative
        atomic_write_json(target, payload)
    atomic_write_json(root_path / truth_file, truth)
    atomic_write_json(data_path / "truth.json", truth)

    invalid_files, invalid_truth = build_invalid_sample()
    invalid_path = data_path / "invalid"
    for relative, payload in invalid_files.items():
        atomic_write_json(invalid_path / relative, payload)
    atomic_write_json(invalid_path / "truth.json", invalid_truth)

    return {
        "data_dir": str(data_path),
        "truth_file": str(root_path / truth_file),
        "invalid_data_dir": str(invalid_path),
        "files_written": len(main_files) + len(invalid_files) + 3,
    }
