from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from failover_console.codes import BusinessCode
from failover_console.engine import FailoverEngine
from failover_console.errors import BusinessError
from failover_console.jsonl_logger import JsonlLogger

from .helpers import SuccessTransport, instance, load_scene, scene_data, service


def auto_data(
    *, scene_version: str = "persist-v1", policy_version: str = "p1", mode: str = "auto"
) -> dict[str, Any]:
    return scene_data(
        [
            service(
                "payments",
                [
                    instance("payments-a", "primary", 49901),
                    instance("payments-b", "standby", 49902),
                    instance("payments-c", "standby", 49903),
                ],
                mode=mode,
                cooldown_ms=60_000,
                min_healthy_replicas=1,
                policy_version=policy_version,
            )
        ],
        scene_version=scene_version,
    )


class PersistenceReplayTests(unittest.TestCase):
    def test_snapshot_restores_roles_versions_cooldown_and_pending(self) -> None:
        scene = load_scene(auto_data(mode="manual"))
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                engine.run_cycle(
                    runtime_faults={"payments-a": "unreachable"},
                    now_epoch_ms_override=6_000_000,
                )
                pending_before = engine.repo.list_pending()
                self.assertEqual(len(pending_before), 1)
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                self.assertTrue(engine.recovery.full_replace)
                self.assertTrue(engine.recovery.scene_version_match)
                self.assertEqual(engine.runtime["payments-a"].role, "primary")
                self.assertEqual(engine.runtime["payments-b"].role, "standby")
                self.assertEqual(engine.runtime["payments-a"].health_state, "unhealthy")
                pending_after = engine.repo.list_pending()
                self.assertEqual(len(pending_after), 1)
                self.assertEqual(pending_after[0]["request_id"], pending_before[0]["request_id"])

    def test_reopen_after_switch_restores_snapshot_and_replays(self) -> None:
        scene = load_scene(auto_data())
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                first = engine.run_cycle(now_epoch_ms_override=7_000_000)
                second = engine.run_cycle(
                    runtime_faults={"payments-a": "unreachable"},
                    now_epoch_ms_override=7_000_100,
                )
                self.assertIn(BusinessCode.SWITCHED.value, second.codes)
                self.assertIsNotNone(second.snapshot_id)
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                self.assertEqual(engine.runtime["payments-a"].role, "standby")
                self.assertEqual(engine.runtime["payments-b"].role, "primary")
                self.assertEqual(engine.runtime["payments-a"].version, 2)
                self.assertTrue(engine.runtime["payments-a"].isolated)
                replay = engine.replay_latest()
                self.assertTrue(replay.ok, [item.as_dict() for item in replay.differences])

    def test_scene_version_mismatch_full_replaces_then_requires_matching_scene(self) -> None:
        old_scene = load_scene(auto_data(scene_version="persist-v1", policy_version="p1"))
        new_scene = load_scene(auto_data(scene_version="persist-v2", policy_version="p2"))
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(old_scene, db, transport=SuccessTransport()) as engine:
                engine.run_cycle(now_epoch_ms_override=8_000_000)
                engine.run_cycle(
                    runtime_faults={"payments-a": "unreachable"},
                    now_epoch_ms_override=8_000_100,
                )
            with FailoverEngine(new_scene, db, transport=SuccessTransport()) as engine:
                self.assertFalse(engine.recovery.scene_version_match)
                self.assertTrue(engine.recovery.full_replace)
                # Snapshot state is replaced wholesale, not merged with the v2 file baseline.
                self.assertEqual(engine.runtime["payments-b"].role, "primary")
                self.assertEqual(engine.services["payments"].policy.version, "p1")
                self.assertIsNotNone(
                    engine.services["payments"].cooldown_started_epoch_ms
                )
                self.assertEqual(
                    {item.health_state for item in engine.runtime.values()}, {"unknown"}
                )
                with self.assertRaises(BusinessError) as ctx:
                    engine.run_cycle()
                self.assertEqual(ctx.exception.code, BusinessCode.E_REPLAY.value)

    def test_unfinished_switch_is_marked_unknown_and_rerun_required(self) -> None:
        scene = load_scene(auto_data())
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                engine.run_cycle(now_epoch_ms_override=9_000_000)
                engine.repo.begin_switch(
                    request_id="tx-incomplete",
                    run_id="run-crashed",
                    service_id="payments",
                    source_instance="payments-a",
                    target_instance="payments-b",
                    trigger_reason="primary_unhealthy",
                    policy_version="p1",
                    code="SWITCHING",
                    details={},
                    now_epoch_ms=9_000_100,
                )
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                self.assertEqual(engine.recovery.incomplete_services, ("payments",))
                self.assertIn("payments", engine.recovery.requires_reprobe_services)
                self.assertTrue(engine.runtime["payments-a"].requires_reprobe)
                self.assertEqual(engine.runtime["payments-a"].health_state, "unknown")
                self.assertEqual(engine.repo.get_switch_transactions(), [])

    def test_health_without_any_probe_record_is_forced_unknown(self) -> None:
        scene = load_scene(auto_data())
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                engine.run_cycle(now_epoch_ms_override=9_500_000)
                self.assertEqual(
                    {item.health_state for item in engine.runtime.values()}, {"healthy"}
                )
                with engine.repo._lock:
                    engine.repo._conn.execute("DELETE FROM probe_attempts")
            with FailoverEngine(scene, db, transport=SuccessTransport()) as engine:
                self.assertIn("payments", engine.recovery.requires_reprobe_services)
                self.assertEqual(
                    {item.health_state for item in engine.runtime.values()}, {"unknown"}
                )
                self.assertTrue(
                    all(item.requires_reprobe for item in engine.runtime.values())
                )

    def test_replay_detects_policy_parameter_mismatch(self) -> None:
        original = load_scene(auto_data(policy_version="p1"))
        changed = load_scene(auto_data(policy_version="p2"))
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            with FailoverEngine(original, db, transport=SuccessTransport()) as engine:
                engine.run_cycle(now_epoch_ms_override=10_000_000)
            with FailoverEngine(changed, db, transport=SuccessTransport()) as engine:
                replay = engine.replay_latest()
                self.assertFalse(replay.ok)
                self.assertEqual(replay.code, BusinessCode.E_REPLAY.value)
                self.assertTrue(
                    any(
                        item.field.endswith("policy.version")
                        for item in replay.differences
                    ),
                    [item.as_dict() for item in replay.differences],
                )

    def test_snapshot_write_failure_is_e_snapshot_and_logged(self) -> None:
        scene = load_scene(auto_data())
        with TemporaryDirectory() as td:
            db = Path(td) / "session.sqlite"
            log = Path(td) / "log.jsonl"
            logger = JsonlLogger(log, echo=False)
            with FailoverEngine(
                scene,
                db,
                logger=logger,
                transport=SuccessTransport(),
                fail_snapshot_writes=True,
            ) as engine:
                with self.assertRaises(BusinessError) as ctx:
                    engine.run_cycle(
                        runtime_faults={"payments-a": "unreachable"},
                        now_epoch_ms_override=11_000_000,
                    )
                self.assertEqual(ctx.exception.code, BusinessCode.E_SNAPSHOT.value)
            records = [
                json.loads(line)
                for line in log.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(
                any(record["code"] == BusinessCode.E_SNAPSHOT.value for record in records)
            )
            # Role/version writes happened before snapshot failure and remain recoverable.
            with FailoverEngine(scene, db, transport=SuccessTransport()) as recovered:
                self.assertEqual(sum(1 for item in recovered.runtime.values() if item.role == "primary"), 1)


if __name__ == "__main__":
    unittest.main()
