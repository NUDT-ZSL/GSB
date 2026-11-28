from __future__ import annotations

import hashlib
import unittest

from exchange_storage.engine import Planner
from exchange_storage.sample import generate_project
from exchange_storage.validation import parse_project


def digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def base_manifest(
    *,
    policy: dict | None = None,
    institutions: list[dict] | None = None,
    regions: list[dict] | None = None,
    objects: list[dict] | None = None,
    events: list[dict] | None = None,
    incidents: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": 1,
        "project_id": "manual-test",
        "seed": 7,
        "policy": policy
        or {
            "watermark_ratio": 0.8,
            "max_latency_ms": 100,
            "urgency_weights": {
                "routine": 1.0,
                "urgent": 2.0,
                "critical": 4.0,
            },
        },
        "institutions": institutions
        or [
            {
                "id": "I1",
                "name": "机构一",
                "required_compliance_level": 1,
            }
        ],
        "regions": regions
        or [
            {
                "id": "R1",
                "capacity_bytes": 10_000,
                "used_bytes": 0,
                "health": "healthy",
                "latency_ms": 5,
                "compliance_level": 3,
            }
        ],
        "objects": objects or [],
        "events": events or [],
        "incidents": incidents or [],
    }


def object_row(
    object_id: str,
    *,
    institution_id: str = "I1",
    urgency: str = "routine",
    size: int = 100,
    created_at: float = 0,
    expires_at: float = 100,
    reference_count: int = 1,
) -> dict:
    return {
        "id": object_id,
        "institution_id": institution_id,
        "test_category": "生化",
        "urgency": urgency,
        "bytes": size,
        "created_at": created_at,
        "expires_at": expires_at,
        "digest": digest(f"{object_id}:{size}:{urgency}"),
        "reference_count": reference_count,
    }


def parse_and_run(manifest: dict) -> tuple[Planner, dict]:
    project, errors = parse_project(manifest)
    if errors or project is None:
        raise AssertionError(errors)
    planner = Planner(project, seed=manifest.get("seed"))
    return planner, planner.run()


class ValidationTests(unittest.TestCase):
    def test_error_list_has_object_and_institution_locators(self) -> None:
        manifest = base_manifest(
            objects=[
                object_row("BAD-SIZE", size=-1, expires_at=0, created_at=2),
                {
                    **object_row("UNKNOWN-ORG"),
                    "institution_id": "NO-SUCH-ORG",
                },
                {**object_row("BAD-URGENCY"), "urgency": "instant"},
            ],
            events=[
                {"at": 1, "type": "expire", "object_id": "MISSING"},
            ],
            incidents=[
                {
                    "at": 2,
                    "type": "health_degraded",
                    "region_id": "NO-SUCH-REGION",
                }
            ],
        )
        project, errors = parse_project(manifest)
        self.assertIsNone(project)
        codes = {error.code for error in errors}
        self.assertIn("INVALID_OBJECT_BYTES", codes)
        self.assertIn("EXPIRES_BEFORE_CREATED", codes)
        self.assertIn("UNKNOWN_INSTITUTION", codes)
        self.assertIn("INVALID_URGENCY", codes)
        self.assertIn("UNKNOWN_EVENT_OBJECT", codes)
        self.assertIn("UNKNOWN_INCIDENT_REGION", codes)
        located = [error for error in errors if error.object_id == "BAD-SIZE"]
        self.assertTrue(located)
        self.assertTrue(all(error.institution_id == "I1" for error in located))

    def test_events_must_be_time_ordered(self) -> None:
        manifest = base_manifest(
            objects=[object_row("O1")],
            events=[
                {"at": 5, "type": "access", "object_id": "O1"},
                {"at": 4, "type": "access", "object_id": "O1"},
            ],
        )
        project, errors = parse_project(manifest)
        self.assertIsNone(project)
        self.assertEqual(
            [error.code for error in errors], ["EVENTS_NOT_TIME_ORDERED"]
        )


class PlacementTests(unittest.TestCase):
    def test_score_filters_and_unselected_reasons(self) -> None:
        manifest = base_manifest(
            policy={
                "watermark_ratio": 0.9,
                "max_latency_ms": 50,
                "urgency_weights": {
                    "routine": 1,
                    "urgent": 2,
                    "critical": 4,
                },
            },
            institutions=[
                {
                    "id": "I1",
                    "required_compliance_level": 2,
                    "max_latency_ms": 50,
                }
            ],
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 3,
                },
                {
                    "id": "R2",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 30,
                    "compliance_level": 1,
                },
                {
                    "id": "R3",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 100,
                    "compliance_level": 3,
                },
            ],
            objects=[object_row("O1", urgency="urgent", size=1000)],
        )
        _, report = parse_and_run(manifest)
        row = report["assignment"]["objects"][0]
        self.assertEqual(row["assigned_region_id"], "R1")
        self.assertEqual(row["selected_score"], 333.333333)
        reasons = {
            candidate["region_id"]: candidate["reason_code"]
            for candidate in row["candidates"]
        }
        self.assertEqual(reasons["R1"], "SELECTED")
        self.assertEqual(reasons["R2"], "EXCLUDED_COMPLIANCE_LEVEL")
        self.assertEqual(reasons["R3"], "EXCLUDED_LATENCY_LIMIT")

    def test_greedy_capacity_repair_moves_smallest_score_loss(self) -> None:
        manifest = base_manifest(
            policy={
                "watermark_ratio": 0.5,
                "max_latency_ms": 100,
                "urgency_weights": {
                    "routine": 1,
                    "urgent": 2,
                    "critical": 4,
                },
            },
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 10,
                    "compliance_level": 2,
                },
                {
                    "id": "R2",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 20,
                    "compliance_level": 2,
                },
            ],
            objects=[
                object_row("O1", size=300),
                object_row("O2", size=300),
                object_row("O3", size=200),
            ],
        )
        _, report = parse_and_run(manifest)
        moves = report["capacity_control"]["repair_moves"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["object_id"], "O1")
        self.assertEqual(moves[0]["source_region_id"], "R1")
        self.assertEqual(moves[0]["target_region_id"], "R2")
        distribution = report[
            "distribution_comparison"
        ]["after_capacity_assignment"]
        self.assertEqual(distribution["over_limit_region_ids"], [])
        self.assertEqual(
            {row["region_id"]: row["effective_used_bytes"] for row in distribution["regions"]},
            {"R1": 500, "R2": 300},
        )

    def test_unresolved_region_reports_eviction_and_prediction(self) -> None:
        manifest = base_manifest(
            policy={
                "watermark_ratio": 0.5,
                "max_latency_ms": 100,
                "urgency_weights": {"routine": 1},
            },
            institutions=[
                {
                    "id": "I1",
                    "required_compliance_level": 2,
                }
            ],
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 1000,
                    "used_bytes": 400,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 2,
                },
                {
                    "id": "R2",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 10,
                    "compliance_level": 1,
                },
            ],
            objects=[object_row("O1", size=200)],
        )
        _, report = parse_and_run(manifest)
        unresolved = report["capacity_control"]["initial_unresolved_regions"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["region_id"], "R1")
        self.assertEqual(unresolved[0]["eviction_object_ids"], ["O1"])
        self.assertEqual(unresolved[0]["predicted_effective_used_bytes"], 400)
        self.assertEqual(unresolved[0]["predicted_watermark"], 0.4)


class TimelineTests(unittest.TestCase):
    def test_expiration_order_and_zero_reference_removal(self) -> None:
        manifest = base_manifest(
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 2,
                }
            ],
            objects=[
                object_row("O-ROUTINE", urgency="routine", reference_count=2),
                object_row("O-CRITICAL", urgency="critical"),
            ],
            events=[
                {"at": 1, "type": "access", "object_id": "O-ROUTINE", "delta": 1},
                {"at": 2, "type": "expire", "object_id": "O-ROUTINE", "delta": 2},
                {"at": 2, "type": "expire", "object_id": "O-CRITICAL", "delta": 1},
                {"at": 3, "type": "expire", "object_id": "O-ROUTINE", "delta": 1},
            ],
        )
        _, report = parse_and_run(manifest)
        at_two = [
            event for event in report["events_processed"] if event["at"] == 2
        ]
        self.assertEqual(
            [event["object_id"] for event in at_two],
            ["O-CRITICAL", "O-ROUTINE"],
        )
        snapshot_two = next(
            row for row in report["timeline"] if row["at"] == 2
        )
        self.assertEqual(snapshot_two["regions"][0]["object_count"], 1)
        self.assertEqual(snapshot_two["regions"][0]["assigned_object_bytes"], 100)
        snapshot_three = next(
            row for row in report["timeline"] if row["at"] == 3
        )
        self.assertEqual(snapshot_three["regions"][0]["object_count"], 0)
        self.assertEqual(
            report["region_reference_lists"][0]["object_count"], 0
        )
        self.assertTrue(report["reconciliation"]["ok"])

    def test_weighted_latency_satisfaction_counts_unassigned_weight(self) -> None:
        manifest = base_manifest(
            policy={
                "watermark_ratio": 0.9,
                "max_latency_ms": 50,
                "urgency_weights": {"routine": 1, "urgent": 2},
            },
            institutions=[
                {"id": "I1", "required_compliance_level": 1},
                {"id": "I2", "required_compliance_level": 3},
            ],
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 2,
                },
                {
                    "id": "R2",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 40,
                    "compliance_level": 1,
                },
            ],
            objects=[
                object_row("O1", institution_id="I1", urgency="urgent", size=100),
                object_row("O2", institution_id="I2", urgency="routine", size=100),
            ],
        )
        _, report = parse_and_run(manifest)
        satisfaction = report["distribution_comparison"][
            "weighted_latency_satisfaction_comparison"
        ]["after_timeline"]
        self.assertEqual(satisfaction["satisfied_weight"], 2.0)
        self.assertEqual(satisfaction["total_weight"], 3.0)
        self.assertEqual(satisfaction["rate_percent"], 66.666667)
        self.assertEqual(
            report["assignment"]["unassigned_object_ids"], ["O2"]
        )
        self.assertTrue(report["reconciliation"]["ok"])


class RelocationTests(unittest.TestCase):
    def _two_region_project(self) -> dict:
        return base_manifest(
            policy={
                "watermark_ratio": 0.5,
                "max_latency_ms": 100,
                "urgency_weights": {"routine": 1, "critical": 4},
            },
            regions=[
                {
                    "id": "R1",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 2,
                },
                {
                    "id": "R2",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 20,
                    "compliance_level": 2,
                },
                {
                    "id": "R3",
                    "capacity_bytes": 1000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 30,
                    "compliance_level": 2,
                },
            ],
            objects=[
                object_row("O1", urgency="critical", size=300),
                object_row("O2", urgency="routine", size=400),
            ],
            incidents=[
                {
                    "at": 2,
                    "type": "health_degraded",
                    "region_id": "R2",
                    "health": "degraded",
                }
            ],
        )

    def test_only_affected_object_moves_and_matches_full_resolve(self) -> None:
        planner, report = parse_and_run(self._two_region_project())
        before = {
            row["object_id"]: row["assigned_region_id"]
            for row in report["assignment"]["objects"]
        }
        self.assertEqual(before["O1"], "R1")
        self.assertEqual(before["O2"], "R2")
        final = dict(planner.assignments)
        self.assertEqual(final["O1"], "R1")
        self.assertEqual(final["O2"], "R3")
        self.assertEqual(report["relocation_progress"]["summary"]["migrated"], 1)

        full_manifest = self._two_region_project()
        full_manifest["incidents"] = []
        for region in full_manifest["regions"]:
            if region["id"] == "R2":
                region["health"] = "degraded"
        _, full_report = parse_and_run(full_manifest)
        full_assignment = {
            row["object_id"]: row["assigned_region_id"]
            for row in full_report["assignment"]["objects"]
        }
        self.assertEqual(final, full_assignment)
        self.assertTrue(report["reconciliation"]["ok"])

    def test_target_health_downgrade_fails_inflight_migration(self) -> None:
        manifest = base_manifest(
            policy={
                "watermark_ratio": 0.9,
                "max_latency_ms": 100,
                "urgency_weights": {"critical": 4},
            },
            regions=[
                {
                    "id": "R-FALLBACK",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 50,
                    "compliance_level": 2,
                },
                {
                    "id": "R-SOURCE",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 5,
                    "compliance_level": 2,
                },
                {
                    "id": "R-TARGET",
                    "capacity_bytes": 10_000,
                    "used_bytes": 0,
                    "health": "healthy",
                    "latency_ms": 10,
                    "compliance_level": 2,
                },
            ],
            objects=[
                object_row("O1", urgency="critical", size=100),
                object_row("O2", urgency="critical", size=100),
            ],
            incidents=[
                {
                    "at": 2,
                    "type": "health_degraded",
                    "region_id": "R-SOURCE",
                    "health": "degraded",
                },
                {
                    "at": 2,
                    "type": "health_degraded",
                    "region_id": "R-TARGET",
                    "health": "degraded",
                    "after_migration_step": 1,
                },
            ],
        )
        _, report = parse_and_run(manifest)
        first_batch = next(
            batch
            for batch in report["relocations"]
            if batch["at"] == 2
            and any(
                trigger.get("kind") == "health"
                for trigger in batch["triggers"]
            )
            and len(batch["plan"]) == 2
        )
        self.assertEqual(
            [entry["status"] for entry in first_batch["plan"]],
            ["migrated", "failed"],
        )
        self.assertEqual(
            first_batch["plan"][1]["failure_reason_code"],
            "TARGET_HEALTH_DEGRADED",
        )
        self.assertEqual(
            first_batch["plan"][1]["target_region_id"], "R-TARGET"
        )
        self.assertTrue(report["reconciliation"]["ok"])

    def test_reconciliation_reports_digest_and_copy_ownership(self) -> None:
        manifest = self._two_region_project()
        manifest["incidents"] = []
        planner, _ = parse_and_run(manifest)
        object_id = "O1"
        region_id = planner.assignments[object_id]
        planner.region_refs[region_id][object_id]["digest"] = digest("tampered")
        planner.region_refs[region_id][object_id]["reference_count"] = 9
        audit = planner._reconcile("tampered")
        self.assertFalse(audit["ok"])
        self.assertEqual(audit["first_inconsistency"]["object_id"], "O1")
        self.assertEqual(audit["first_inconsistency"]["region_id"], region_id)
        codes = {
            issue["code"]
            for record in audit["inconsistencies"]
            for issue in record["issues"]
        }
        self.assertIn("DIGEST_MISMATCH", codes)
        self.assertIn("REFERENCE_COUNT_MISMATCH", codes)

        duplicate_manifest = self._two_region_project()
        duplicate_manifest["incidents"] = []
        duplicate_planner, _ = parse_and_run(duplicate_manifest)
        duplicated_object = "O1"
        real_region = duplicate_planner.assignments[duplicated_object]
        other_region = next(
            region_id
            for region_id in duplicate_planner.regions
            if region_id != real_region
        )
        duplicate_planner.region_refs[other_region][duplicated_object] = dict(
            duplicate_planner.region_refs[real_region][duplicated_object]
        )
        duplicate_audit = duplicate_planner._reconcile("duplicate")
        duplicate_codes = {
            issue["code"]
            for record in duplicate_audit["inconsistencies"]
            for issue in record["issues"]
        }
        self.assertIn("DUPLICATE_REFERENCE", duplicate_codes)


class SampleTests(unittest.TestCase):
    def test_fixed_seed_sample_is_deterministic_and_end_to_end(self) -> None:
        first = generate_project(20261128)
        second = generate_project(20261128)
        self.assertEqual(first, second)
        self.assertNotEqual(first, generate_project(20261129))
        _, report = parse_and_run(first)
        self.assertTrue(report["reconciliation"]["ok"])
        self.assertIn(
            "TARGET_HEALTH_DEGRADED",
            {
                entry["failure_reason_code"]
                for entry in report["relocation_progress"]["entries"]
            },
        )
        self.assertEqual(
            set(report["distribution_comparison"]),
            {
                "before_placement",
                "preferred_unconstrained",
                "after_capacity_assignment",
                "after_timeline",
                "over_limit_change",
                "weighted_latency_satisfaction_comparison",
            },
        )
        self.assertEqual(
            report["assignment"]["region_totals"],
            report["distribution_comparison"][
                "after_capacity_assignment"
            ]["regions"],
        )
        self.assertEqual(report["relocation_progress"]["summary"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
