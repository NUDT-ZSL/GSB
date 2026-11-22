from __future__ import annotations

import json
from typing import Any

from .ledger import Ledger, canonical_json, sha256_text
from .models import Card, LeaseConfig, effective_alignment


class Verification:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self.checks: list[dict[str, Any]] = []

    def _check(self, name: str, ok: bool, details: Any = None) -> None:
        entry = {"check": name, "ok": bool(ok)}
        if not ok:
            entry["details"] = details
        self.checks.append(entry)

    def run(self) -> dict[str, Any]:
        with self.ledger.lock():
            connection = self.ledger.connect()
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                self._check("sqlite_integrity", integrity == "ok", integrity)
                meta = {
                    row["key"]: row["value"]
                    for row in connection.execute("SELECT * FROM meta")
                }
                config = LeaseConfig(
                    quota_group_compute_limits={
                        key: int(value)
                        for key, value in json.loads(meta["config_json"])[
                            "quota_group_compute_limits"
                        ].items()
                    },
                    alignment_granularity=int(
                        json.loads(meta["config_json"])["alignment_granularity"]
                    ),
                    renewal_grace_ms=int(
                        json.loads(meta["config_json"])["renewal_grace_ms"]
                    ),
                    fragmentation_threshold=float(
                        json.loads(meta["config_json"])["fragmentation_threshold"]
                    ),
                    clock_steps_ms=tuple(
                        int(value)
                        for value in json.loads(meta["config_json"])["clock_steps_ms"]
                    ),
                    clock_rollback_tolerance_ms=int(
                        json.loads(meta["config_json"]).get(
                            "clock_rollback_tolerance_ms", 0
                        )
                    ),
                )
                cards = [
                    Card(
                        card_id=row["card_id"],
                        total_compute_shares=row["total_compute_shares"],
                        total_memory_shares=row["total_memory_shares"],
                        granularity=row["granularity"],
                        health=row["health"],
                        topology_domain=row["topology_domain"],
                    )
                    for row in connection.execute("SELECT * FROM cards ORDER BY card_id")
                ]
                leases = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM leases ORDER BY lease_id"
                    )
                ]
                active_leases = [
                    lease for lease in leases if lease["status"] == "active"
                ]
                cards_by_id = {card.card_id: card for card in cards}

                for card in cards:
                    alignment = effective_alignment(card, config)
                    for dimension, total in (
                        ("compute", card.total_compute_shares),
                        ("memory", card.total_memory_shares),
                    ):
                        rows = [
                            dict(row)
                            for row in connection.execute(
                                "SELECT * FROM occupancy_ranges "
                                "WHERE card_id = ? AND dimension = ? "
                                "ORDER BY start_share",
                                (card.card_id, dimension),
                            )
                        ]
                        previous_end = 0
                        overlap = None
                        aligned = True
                        bounded = True
                        for row in rows:
                            if row["start_share"] < previous_end:
                                overlap = {
                                    "start": row["start_share"],
                                    "previous_end": previous_end,
                                }
                            if (
                                row["start_share"] % alignment
                                or row["length_share"] % alignment
                            ):
                                aligned = False
                            if row["start_share"] + row["length_share"] > total:
                                bounded = False
                            previous_end = max(
                                previous_end,
                                row["start_share"] + row["length_share"],
                            )
                        self._check(
                            f"{card.card_id}.{dimension}.non_overlap",
                            overlap is None,
                            overlap,
                        )
                        self._check(
                            f"{card.card_id}.{dimension}.alignment",
                            aligned,
                            {"effective_alignment": alignment},
                        )
                        self._check(
                            f"{card.card_id}.{dimension}.within_total",
                            bounded and previous_end <= total,
                            {"occupied_end": previous_end, "total": total},
                        )
                        self._check(
                            f"{card.card_id}.{dimension}.share_conservation",
                            all(
                                lease[dimension + "_start"] % alignment == 0
                                and lease[dimension + "_length"] % alignment == 0
                                for lease in active_leases
                                if lease["card_id"] == card.card_id
                            ),
                            {"active_range_count": len(rows)},
                        )
                        occupied_sum = sum(row["length_share"] for row in rows)
                        self._check(
                            f"{card.card_id}.{dimension}.occupied_sum",
                            occupied_sum <= total,
                            {"occupied_sum": occupied_sum, "total": total},
                        )

                active_ids = {lease["lease_id"] for lease in active_leases}
                range_counts = {
                    row["lease_id"]: row["count"]
                    for row in connection.execute(
                        "SELECT lease_id, COUNT(*) AS count FROM occupancy_ranges "
                        "GROUP BY lease_id"
                    )
                }
                slot_counts = {
                    row["lease_id"]: row["count"]
                    for row in connection.execute(
                        "SELECT lease_id, COUNT(*) AS count FROM occupancy_slots "
                        "GROUP BY lease_id"
                    )
                }
                occupancy_conserved = True
                range_detail: list[dict[str, Any]] = []
                for lease in active_leases:
                    card = cards_by_id[lease["card_id"]]
                    alignment = effective_alignment(card, config)
                    expected_slots = (
                        lease["compute_length"] // alignment
                        + lease["memory_length"] // alignment
                    )
                    if (
                        range_counts.get(lease["lease_id"], 0) != 2
                        or slot_counts.get(lease["lease_id"], 0) != expected_slots
                    ):
                        occupancy_conserved = False
                        range_detail.append(
                            {
                                "lease_id": lease["lease_id"],
                                "ranges": range_counts.get(lease["lease_id"], 0),
                                "slots": slot_counts.get(lease["lease_id"], 0),
                                "expected_slots": expected_slots,
                            }
                        )
                non_active_occupancy = connection.execute(
                    "SELECT COUNT(*) FROM occupancy_ranges WHERE lease_id NOT IN "
                    "(SELECT lease_id FROM leases WHERE status = 'active')"
                ).fetchone()[0]
                self._check(
                    "active_lease_occupancy_mapping",
                    occupancy_conserved and non_active_occupancy == 0,
                    {
                        "detail": range_detail,
                        "stale_rows": non_active_occupancy,
                        "active_ids": sorted(active_ids),
                    },
                )
                slot_set_ok = True
                slot_set_detail: list[dict[str, Any]] = []
                for lease in active_leases:
                    card = cards_by_id[lease["card_id"]]
                    alignment = effective_alignment(card, config)
                    for dimension in ("compute", "memory"):
                        start = lease[dimension + "_start"]
                        length = lease[dimension + "_length"]
                        actual = {
                            row[0]
                            for row in connection.execute(
                                "SELECT slot_index FROM occupancy_slots "
                                "WHERE lease_id = ? AND dimension = ?",
                                (lease["lease_id"], dimension),
                            )
                        }
                        expected = set(
                            range(
                                start // alignment,
                                (start + length) // alignment,
                            )
                        )
                        if actual != expected:
                            slot_set_ok = False
                            slot_set_detail.append(
                                {
                                    "lease_id": lease["lease_id"],
                                    "dimension": dimension,
                                    "expected": sorted(expected),
                                    "actual": sorted(actual),
                                }
                            )
                self._check(
                    "occupancy_slot_sets_match_ranges",
                    slot_set_ok,
                    slot_set_detail,
                )

                quota_ok = True
                quota_detail: list[dict[str, Any]] = []
                for row in connection.execute(
                    "SELECT * FROM quota_groups ORDER BY quota_group"
                ):
                    active_sum = connection.execute(
                        "SELECT COALESCE(SUM(compute_length), 0) FROM leases "
                        "WHERE quota_group = ? AND status = 'active'",
                        (row["quota_group"],),
                    ).fetchone()[0]
                    ok = (
                        active_sum == row["consumed_compute"]
                        and row["consumed_compute"] <= row["limit_compute"]
                    )
                    quota_ok = quota_ok and ok
                    if not ok:
                        quota_detail.append(
                            {
                                "quota_group": row["quota_group"],
                                "counter": row["consumed_compute"],
                                "active_sum": active_sum,
                                "limit": row["limit_compute"],
                            }
                        )
                self._check("quota_share_conservation", quota_ok, quota_detail)

                refunds = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM refunds ORDER BY refund_id"
                    )
                ]
                lease_status = {lease["lease_id"]: lease["status"] for lease in leases}
                refund_ok = True
                refund_detail: list[dict[str, Any]] = []
                for refund in refunds:
                    ok = (
                        refund["consumed_compute"] + refund["refund_compute"]
                        == refund["allocated_compute"]
                        and refund["consumed_memory"] + refund["refund_memory"]
                        == refund["allocated_memory"]
                        and refund["status"] == "pending"
                        and lease_status.get(refund["lease_id"]) == "preempted"
                    )
                    refund_ok = refund_ok and ok
                    if not ok:
                        refund_detail.append(refund)
                active_refunds = connection.execute(
                    "SELECT COUNT(*) FROM refunds r JOIN leases l ON l.lease_id = r.lease_id "
                    "WHERE l.status = 'active'"
                ).fetchone()[0]
                self._check(
                    "pending_refund_ledger",
                    refund_ok and active_refunds == 0,
                    {"invalid": refund_detail, "active_refunds": active_refunds},
                )

                version = int(meta["ledger_version"])
                round_count = connection.execute(
                    "SELECT COUNT(*) FROM rounds"
                ).fetchone()[0]
                max_version = connection.execute(
                    "SELECT COALESCE(MAX(ledger_version), 0) FROM rounds"
                ).fetchone()[0]
                self._check(
                    "ledger_version_monotonic",
                    version == round_count == max_version,
                    {
                        "meta_version": version,
                        "round_count": round_count,
                        "max_round_version": max_version,
                    },
                )

                report_ok = True
                report_detail: list[dict[str, Any]] = []
                for row in connection.execute(
                    "SELECT * FROM rounds ORDER BY ledger_version"
                ):
                    report = json.loads(row["report_json"])
                    body = {
                        key: value
                        for key, value in report.items()
                        if key != "report_digest"
                    }
                    expected_digest = sha256_text(canonical_json(body))
                    path = self.ledger.report_path(row["commit_key"])
                    disk = None
                    if path.exists():
                        try:
                            disk = json.loads(path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            disk = None
                    ok = (
                        report.get("report_digest") == expected_digest
                        and row["report_digest"] == expected_digest
                        and disk == report
                    )
                    report_ok = report_ok and ok
                    if not ok:
                        report_detail.append(
                            {
                                "commit_key": row["commit_key"],
                                "file_exists": path.exists(),
                                "digest_match": report.get("report_digest")
                                == expected_digest,
                            }
                        )
                self._check("round_report_digests", report_ok, report_detail)

                latest = self.ledger.latest_round(connection)
                pointer = self.ledger.pointer_snapshot()
                pointer_ok = pointer is not None and pointer.get("version") == version
                if latest is not None:
                    pointer_ok = bool(
                        pointer_ok
                        and pointer.get("commit_key") == latest["commit_key"]
                        and pointer.get("report_digest") == latest["report_digest"]
                        and pointer.get("state_digest") == latest["state_digest"]
                    )
                self._check(
                    "atomic_pointer",
                    pointer_ok,
                    {
                        "pointer": pointer,
                        "db_version": version,
                        "latest_commit": latest["commit_key"] if latest else None,
                    },
                )

                current_state_digest = self.ledger.state_digest(connection)
                state_ok = latest is None or latest["state_digest"] == current_state_digest
                self._check(
                    "current_state_digest",
                    state_ok,
                    {
                        "current": current_state_digest,
                        "latest_round": latest["state_digest"] if latest else None,
                    },
                )

                failures = [check for check in self.checks if not check["ok"]]
                return {
                    "ok": not failures,
                    "checks": self.checks,
                    "failure_count": len(failures),
                    "ledger_version": version,
                    "latest_commit_key": latest["commit_key"] if latest else None,
                }
            finally:
                connection.close()
