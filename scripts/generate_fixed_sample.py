#!/usr/bin/env python3
"""Generate the checked-in deterministic sample and its content digests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_maintenance.canonical import sha256_json  # noqa: E402


def row(
    row_id: int,
    tenant: str,
    customer_id: int,
    status: str,
    region: str,
    amount: str,
    note: str,
    email: str,
) -> dict[str, object]:
    return {
        "id": row_id,
        "tenant": tenant,
        "customer_id": customer_id,
        "status": status,
        "region": region,
        "amount": amount,
        "note": note,
        "email": email,
        "version_token": row_id * 10,
    }


INSERT_ROWS = [
    row(1, "A", 1, "NEW", "R1", "10.500", "n1", "a1@example.test"),
    row(2, "A", 2, "NEW", "R2", "20.500", "n2", "a2@example.test"),
    row(3, "B", 1, "ACTIVE", "R1", "30.500", "n3", "b1@example.test"),
    row(4, "B", 2, "DONE", "R1", "40.500", "n4", "b2@example.test"),
    row(5, "A", 3, "HOLD", "R1", "50.500", "n5", "a3@example.test"),
    row(6, "A", 4, "HOLD", "R2", "60.500", "n6", "a4@example.test"),
    row(7, "B", 3, "DONE", "R2", "70.500", "n7", "b3@example.test"),
    row(8, "B", 4, "NEW", "R3", "80.500", "n8", "b4@example.test"),
    row(9, "A", 5, "ACTIVE", "R2", "90.500", "n9", "a5@example.test"),
    row(10, "B", 5, "HOLD", "R3", "100.500", "n10", "b5@example.test"),
    row(11, "A", 6, "DONE", "R3", "110.500", "n11", "a6@example.test"),
    row(12, "B", 6, "NEW", "R4", "120.500", "n12", "b6@example.test"),
]


def operation(
    seq: int,
    op_type: str,
    pk: dict[str, object],
    before: dict[str, object] | None,
    after: dict[str, object] | None,
) -> dict[str, object]:
    payload = {
        "seq": seq,
        "type": op_type,
        "pk": pk,
        "before": before,
        "after": after,
    }
    return {**payload, "content_digest": sha256_json(payload)}


def build_document() -> dict[str, object]:
    operations: list[dict[str, object]] = []
    for position, current in enumerate(INSERT_ROWS, start=1):
        operations.append(
            operation(
                position,
                "insert",
                {"id": current["id"]},
                None,
                current,
            )
        )

    row1 = INSERT_ROWS[0]
    row3 = INSERT_ROWS[2]
    row4 = INSERT_ROWS[3]
    update_after = dict(row3)
    update_after.update(
        {
            "status": "HOLD",
            "region": "R4",
            "amount": "31.250",
        }
    )
    note_after = dict(row4)
    note_after["note"] = "note4-updated"
    operations.extend(
        [
            operation(13, "delete", {"id": 1}, row1, None),
            operation(14, "update", {"id": 3}, row3, update_after),
            operation(15, "update", {"id": 4}, row4, note_after),
        ]
    )

    return {
        "format_version": 1,
        "sample_id": "windows-deterministic-index-sample-v1",
        "schema": {
            "name": "orders",
            "columns": [
                {"name": "id", "type": "INT", "nullable": False},
                {"name": "tenant", "type": "TEXT", "nullable": False},
                {"name": "customer_id", "type": "INT", "nullable": False},
                {"name": "status", "type": "TEXT", "nullable": False},
                {"name": "region", "type": "TEXT", "nullable": False},
                {"name": "amount", "type": "DECIMAL", "nullable": False},
                {"name": "note", "type": "TEXT", "nullable": True},
                {"name": "email", "type": "TEXT", "nullable": False},
                {"name": "version_token", "type": "INT", "nullable": False},
            ],
            "primary_key": ["id"],
            "unique_constraints": [["tenant", "email"]],
        },
        "indexes": [
            {
                "name": "idx_status_region",
                "columns": ["status", "region"],
                "unique": False,
                "covering": ["amount", "email"],
                "covering_query": True,
            },
            {
                "name": "idx_customer",
                "columns": ["customer_id"],
                "unique": False,
                "covering": ["status", "amount"],
                "covering_query": True,
            },
            {
                "name": "idx_region_order",
                "columns": ["region", "id"],
                "unique": True,
                "covering": ["amount"],
                "covering_query": True,
            },
            {
                "name": "idx_email_lookup",
                "columns": ["tenant", "email"],
                "unique": True,
                "covering": ["customer_id"],
                "covering_query": True,
            },
        ],
        "query_templates": [
            {
                "name": "q_status_region",
                "index": "idx_status_region",
                "columns": ["status", "region", "amount", "email"],
            },
            {
                "name": "q_customer",
                "index": "idx_customer",
                "columns": ["customer_id", "status", "amount"],
            },
            {
                "name": "q_region",
                "index": "idx_region_order",
                "columns": ["region", "id", "amount"],
            },
            {
                "name": "q_email",
                "index": "idx_email_lookup",
                "columns": ["tenant", "email", "customer_id"],
            },
        ],
        "operations": operations,
        "config": {
            "page_capacity": 4,
            "split_factor": 0.5,
            "rebuild_threshold": {
                "max_pending_items": 2,
                "max_touched_pages": 1,
            },
            "fixed_scale": 3,
            "rounding": "HALF_UP",
            "float_tolerance": "0.001",
            "clock_tolerance_ms": 2,
            "logical_start_ms": 100000,
            "clock_steps_ms": [
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
            ],
            "rebuild_window_ms": 25,
            "max_tree_height": 8,
        },
    }


def main() -> None:
    target = ROOT / "samples" / "fixed_sample.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(build_document(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(target)


if __name__ == "__main__":
    main()
