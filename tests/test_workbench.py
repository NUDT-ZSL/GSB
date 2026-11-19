from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from obs_workbench.samples import compute_patch_digest


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "samples" / "topology.json"
EVENTS = ROOT / "samples" / "events.json"
PATCH = ROOT / "samples" / "patch.json"
ANOMALIES = ROOT / "samples" / "anomalies"


def run_cli(
    db: Path,
    *args: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[int, str]:
    command = [sys.executable, "-m", "obs_workbench", "--db", str(db), *args]
    environment = os.environ.copy()
    environment.pop("OBS_FAILPOINT", None)
    if env:
        environment.update(env)
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    output = completed.stdout.strip()
    if check and completed.returncode != 0:
        raise AssertionError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed.returncode, output


def run_json(db: Path, *args: str, env: dict[str, str] | None = None) -> dict:
    _, output = run_cli(db, *args, env=env)
    return json.loads(output)


def status_bytes(db: Path) -> bytes:
    return run_cli(db, "status")[1].encode("utf-8")


def status(db: Path) -> dict:
    return json.loads(status_bytes(db))


def prepare(db: Path, *, with_events: bool = True) -> None:
    run_json(db, "load", "--topology", str(TOPOLOGY))
    if with_events:
        run_json(db, "ingest", "--events", str(EVENTS))


def window(database: Path, instance: str, rule: str, start: int) -> dict:
    for item in status(database)["windows"]:
        if (
            item["instance_id"] == instance
            and item["rule_name"] == rule
            and item["window_start_ms"] == start
        ):
            return item
    raise AssertionError(f"window not found: {instance}/{rule}/{start}")


class WorkbenchEndToEndTests(unittest.TestCase):
    def test_removed_silence_releases_flag_bound_suppression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "bound.sqlite3"
            topology = root / "topology.json"
            events = root / "events.json"
            patch = root / "patch.json"
            topology.write_text(
                json.dumps(
                    {
                        "topology_version": 1,
                        "effective_clock_ms": 0,
                        "window_ms": 10000,
                        "allowed_lateness_ms": 0,
                        "services": [
                            {"service_name": "a", "upstream_services": ["b"]},
                            {"service_name": "b", "upstream_services": []},
                        ],
                        "instances": [
                            {"instance_id": "a-1", "service_name": "a", "weight": 1.0},
                            {"instance_id": "b-1", "service_name": "b", "weight": 1.0},
                        ],
                        "silence_windows": [
                            {
                                "silence_id": "sil-bound",
                                "instance_id": "a-1",
                                "rule_name": "R",
                                "start_clock_ms": 1000,
                                "end_clock_ms": 3000,
                                "kind": "maintenance",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            events.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "event_seq": 1,
                                "instance_id": "a-1",
                                "rule_name": "R",
                                "severity": "critical",
                                "occurred_clock_ms": 2000,
                                "arrived_clock_ms": 1000,
                                "metric_value": 99,
                                "silenced_maintenance": True,
                            },
                            {
                                "event_seq": 2,
                                "instance_id": "a-1",
                                "rule_name": "R",
                                "severity": "warning",
                                "occurred_clock_ms": 20000,
                                "arrived_clock_ms": 2000,
                                "metric_value": 10,
                                "silenced_maintenance": False,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            changes = {
                "dependency_add": [],
                "dependency_remove": [],
                "instance_weights": [],
                "silence_add": [],
                "silence_remove": ["sil-bound"],
            }
            patch_body = {
                "parent_topology_version": 1,
                "patched_topology_version": 2,
                "effective_clock_ms": 30000,
                "changes": changes,
            }
            patch_body["content_digest"] = compute_patch_digest(patch_body)
            patch.write_text(json.dumps(patch_body), encoding="utf-8")

            run_json(db, "load", "--topology", str(topology))
            run_json(db, "ingest", "--events", str(events))
            before = window(db, "a-1", "R", 0)
            self.assertEqual(before["result"]["conclusion"], "SUPPRESSED_ONLY")
            run_json(db, "apply", "--patch", str(patch))
            after = window(db, "a-1", "R", 0)
            self.assertEqual(after["result"]["conclusion"], "ACTIONABLE")
            self.assertEqual(after["result"]["suppressed_count"], 0)
            self.assertEqual(after["result"]["representative_event"]["event_seq"], 1)
            self.assertTrue(run_json(db, "verify")["ok"])

    def test_event_time_windows_dedup_evidence_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db)
            view = status(db)

            self.assertEqual(view["topology"]["published_topology_version"], 1)
            self.assertTrue(view["current_complete"])
            watermarks = {
                (item["instance_id"], item["rule_name"]): item["watermark_clock_ms"]
                for item in view["watermarks"]
            }
            self.assertEqual(watermarks[("pay-2", "CPUThrottle")], 210000)
            self.assertEqual(watermarks[("inv-1", "DiskLatency")], 200000)

            pay2 = window(db, "pay-2", "CPUThrottle", 0)
            self.assertEqual(pay2["state"], "CLOSED")
            self.assertEqual(pay2["closed_at_watermark_ms"], 105000)
            self.assertEqual(pay2["result"]["raw_event_count"], 4)
            self.assertEqual(pay2["result"]["deduplicated_event_count"], 3)
            self.assertEqual(pay2["result"]["duplicate_event_count"], 1)
            self.assertEqual(pay2["result"]["suppressed_count"], 2)
            self.assertEqual(pay2["result"]["representative_event"]["event_seq"], 11)
            self.assertEqual(pay2["late_ledger_count"], 1)
            self.assertTrue(
                all(item["topology_version"] == 1 for item in pay2["evidence_chain"])
            )
            self.assertEqual(
                [item["service_name"] for item in pay2["evidence_chain"]],
                ["payment-api", "ledger-db"],
            )

            inventory = window(db, "inv-1", "DiskLatency", 0)
            self.assertEqual(inventory["result"]["suppressed_count"], 1)
            self.assertEqual(inventory["result"]["representative_event"]["event_seq"], 30)
            self.assertEqual(
                sorted(item["event_seq"] for item in view["late_ledger"]),
                [13, 23, 31],
            )

            frontend = window(db, "fe-1", "RequestError", 0)
            self.assertEqual(
                {item["service_name"] for item in frontend["evidence_chain"]},
                {"frontend-api", "edge-api", "payment-api", "inventory-api", "ledger-db"},
            )

            self.assertEqual(status_bytes(db), status_bytes(db))
            verified = run_json(db, "verify")
            self.assertTrue(verified["ok"], verified)

    def test_patch_invalidation_history_reuse_and_late_reaudit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db)
            before = status(db)
            billing_before = window(db, "bill-1", "QueueDepth", 0)

            applied = run_json(db, "apply", "--patch", str(PATCH))
            self.assertFalse(applied["idempotent"])
            after = status(db)
            self.assertEqual(after["topology"]["published_topology_version"], 2)

            billing_after = window(db, "bill-1", "QueueDepth", 0)
            self.assertEqual(billing_after["conclusion_topology_version"], 1)
            self.assertEqual(
                json.dumps(billing_after["result"], sort_keys=True, ensure_ascii=False),
                json.dumps(billing_before["result"], sort_keys=True, ensure_ascii=False),
            )
            self.assertEqual(
                json.dumps(billing_after["evidence_chain"], sort_keys=True, ensure_ascii=False),
                json.dumps(billing_before["evidence_chain"], sort_keys=True, ensure_ascii=False),
            )
            self.assertEqual([item["topology_version"] for item in billing_after["history"]], [1])

            edge = window(db, "edge-1", "RuleThreshold", 0)
            self.assertEqual(edge["conclusion_topology_version"], 2)
            self.assertEqual([item["topology_version"] for item in edge["history"]], [1, 2])
            self.assertEqual(
                {item["topology_version"] for item in edge["evidence_chain"]},
                {2},
            )
            self.assertNotIn(
                "inventory-api",
                {item["service_name"] for item in edge["evidence_chain"]},
            )

            pay2_middle = window(db, "pay-2", "CPUThrottle", 60000)
            self.assertEqual(pay2_middle["result"]["conclusion"], "SUPPRESSED_ONLY")
            self.assertEqual(pay2_middle["result"]["suppressed_count"], 2)
            self.assertIsNone(pay2_middle["result"]["representative_event"])
            self.assertEqual(pay2_middle["result"]["reevaluated_late_event_seqs"], [23])

            inventory = window(db, "inv-1", "DiskLatency", 0)
            self.assertEqual(inventory["result"]["representative_event"]["event_seq"], 31)
            self.assertEqual(inventory["result"]["reevaluated_late_event_seqs"], [31])
            self.assertEqual(inventory["result"]["suppressed_count"], 0)

            edge_invalidations = [
                item for item in after["invalidations"] if item["window_key"].startswith("edge-1")
            ]
            self.assertTrue(edge_invalidations)
            closed_edge = next(
                item for item in edge_invalidations if item["window_key"].endswith("\u00000\u000059999")
            )
            self.assertEqual(closed_edge["actions"]["conclusion"], "REUSE")
            self.assertEqual(closed_edge["actions"]["evidence_chain"], "REBUILD")
            self.assertEqual(closed_edge["actions"]["late_ledger"], "REUSE")
            self.assertEqual(closed_edge["patch_version"], 2)
            self.assertEqual(closed_edge["propagation_paths"], [["edge-api"]])
            self.assertIn("dependency_remove", [item["kind"] for item in closed_edge["triggers"]])

            snapshot_after_apply = status_bytes(db)
            repeated = run_json(db, "apply", "--patch", str(PATCH))
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(status_bytes(db), snapshot_after_apply)
            self.assertTrue(run_json(db, "verify")["ok"])
            self.assertEqual(
                [item["topology_version"] for item in window(db, "fe-1", "RequestError", 0)["history"]],
                [1, 2],
            )
            self.assertNotEqual(before["topology"]["content_digest"], after["topology"]["content_digest"])

    def test_reingest_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db)
            before = status_bytes(db)
            result = run_json(db, "ingest", "--events", str(EVENTS))
            self.assertEqual(result["processed_events"], 0)
            self.assertEqual(result["skipped_events"], 20)
            self.assertEqual(status_bytes(db), before)

    def test_late_event_after_patch_does_not_rewrite_closed_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "late-after-patch.sqlite3"
            late_file = root / "late.json"
            prepare(db)
            run_json(db, "apply", "--patch", str(PATCH))
            before = window(db, "pay-2", "CPUThrottle", 0)
            late_file.write_text(
                json.dumps(
                    {
                        "events": [
                            {
                                "event_seq": 999,
                                "instance_id": "pay-2",
                                "rule_name": "CPUThrottle",
                                "severity": "critical",
                                "occurred_clock_ms": 40000,
                                "arrived_clock_ms": 300000,
                                "metric_value": 100,
                                "silenced_maintenance": False,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            ingested = run_json(db, "ingest", "--events", str(late_file))
            self.assertEqual(ingested["processed_events"], 1)
            after = window(db, "pay-2", "CPUThrottle", 0)
            self.assertEqual(after["current_revision_digest"], before["current_revision_digest"])
            self.assertEqual(after["result"], before["result"])
            self.assertEqual(after["late_ledger_count"], before["late_ledger_count"] + 1)
            self.assertTrue(run_json(db, "verify")["ok"])


class ValidationAndIsolationTests(unittest.TestCase):
    def test_invalid_inputs_have_distinct_codes_and_no_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db)
            baseline = status_bytes(db)

            code, output = run_cli(
                db, "ingest", "--events", str(ANOMALIES / "events_unknown_instance.json"), check=False
            )
            self.assertEqual(json.loads(output)["error"]["code"], "E_EVENT_UNKNOWN_INSTANCE")
            self.assertEqual(code, 2)
            self.assertEqual(status_bytes(db), baseline)

            for filename, expected in [
                ("patch_unknown_instance.json", "E_PATCH_UNKNOWN_INSTANCE"),
                ("patch_silence_reversed.json", "E_SILENCE_RANGE_INVALID"),
                ("patch_dependency_cycle.json", "E_TOPOLOGY_CYCLE"),
                ("patch_digest_mismatch.json", "E_PATCH_DIGEST_MISMATCH"),
            ]:
                code, output = run_cli(
                    db, "apply", "--patch", str(ANOMALIES / filename), check=False
                )
                self.assertEqual(code, 2)
                self.assertEqual(json.loads(output)["error"]["code"], expected)
                self.assertEqual(status_bytes(db), baseline)

            rollback_db = Path(directory) / "rollback.sqlite3"
            prepare(rollback_db)
            run_json(rollback_db, "apply", "--patch", str(PATCH))
            code, output = run_cli(
                rollback_db,
                "apply",
                "--patch",
                str(ANOMALIES / "patch_version_rollback.json"),
                check=False,
            )
            self.assertEqual(code, 2)
            self.assertEqual(
                json.loads(output)["error"]["code"], "E_TOPOLOGY_VERSION_ROLLBACK"
            )
            self.assertEqual(status(rollback_db)["topology"]["published_topology_version"], 2)

    def test_invalid_topology_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "missing.sqlite3"
            code, output = run_cli(
                db,
                "load",
                "--topology",
                str(ANOMALIES / "topology_cycle.json"),
                check=False,
            )
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(output)["error"]["code"], "E_TOPOLOGY_CYCLE")
            self.assertFalse(db.exists())

    def test_window_digest_mismatch_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db)
            connection = sqlite3.connect(db)
            revision_id = connection.execute(
                "SELECT revision_id FROM window_revisions ORDER BY revision_id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE window_revisions SET digest = 'sha256:tampered' WHERE revision_id = ?",
                (revision_id,),
            )
            connection.commit()
            connection.close()

            code, output = run_cli(db, "verify", check=False)
            self.assertEqual(code, 2)
            verified = json.loads(output)
            self.assertFalse(verified["ok"])
            self.assertIn(
                "E_WINDOW_DIGEST_MISMATCH",
                {item["code"] for item in verified["errors"]},
            )

    def test_bucket_isolation_lets_other_instances_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "workbench.sqlite3"
            prepare(db, with_events=False)
            mixed = Path(directory) / "mixed.json"
            mixed.write_text(
                json.dumps(
                    {
                        "buckets": [
                            {
                                "bucket_id": "bad",
                                "events": [
                                    {
                                        "event_seq": 900,
                                        "instance_id": "ghost",
                                        "rule_name": "QueueDepth",
                                        "severity": "critical",
                                        "occurred_clock_ms": 1000,
                                        "arrived_clock_ms": 2000,
                                        "metric_value": 99,
                                        "silenced_maintenance": False,
                                    }
                                ],
                            },
                            {
                                "bucket_id": "good",
                                "events": [
                                    {
                                        "event_seq": 901,
                                        "instance_id": "bill-1",
                                        "rule_name": "QueueDepth",
                                        "severity": "warning",
                                        "occurred_clock_ms": 1000,
                                        "arrived_clock_ms": 3000,
                                        "metric_value": 45,
                                        "silenced_maintenance": False,
                                    }
                                ],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            code, output = run_cli(
                db,
                "ingest",
                "--events",
                str(mixed),
                "--isolate-buckets",
                check=False,
            )
            self.assertEqual(code, 2)
            result = json.loads(output)
            self.assertFalse(result["command_ok"])
            self.assertEqual(result["processed_events"], 1)
            self.assertTrue(
                any(
                    item.get("error", {}).get("code") == "E_EVENT_UNKNOWN_INSTANCE"
                    for item in result["isolated_buckets"]
                )
            )
            view = status(db)
            self.assertEqual(view["watermarks"], [
                {
                    "instance_id": "bill-1",
                    "rule_name": "QueueDepth",
                    "watermark_clock_ms": 1000,
                }
            ])


class RecoveryAndConcurrencyTests(unittest.TestCase):
    def test_crash_during_patch_propagation_resumes_to_identical_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "interrupted.sqlite3"
            clean = Path(directory) / "clean.sqlite3"
            prepare(db)
            prepare(clean)

            code, _ = run_cli(
                db,
                "apply",
                "--patch",
                str(PATCH),
                env={"OBS_FAILPOINT": "apply_after_invalidation_2"},
                check=False,
            )
            self.assertEqual(code, 97)
            interrupted = status(db)
            self.assertEqual(interrupted["topology"]["published_topology_version"], 1)
            self.assertFalse(interrupted["current_complete"])
            self.assertTrue(interrupted["pending_jobs"])
            self.assertEqual(
                {item["conclusion_topology_version"] for item in interrupted["windows"] if item["conclusion_topology_version"]},
                {1},
            )

            resumed = run_json(db, "resume")
            self.assertEqual(resumed["published_topology_version"], 2)
            run_json(clean, "apply", "--patch", str(PATCH))
            self.assertEqual(status_bytes(db), status_bytes(clean))
            self.assertTrue(run_json(db, "verify")["ok"])

    def test_crash_during_window_close_rolls_back_and_replays_without_double_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "close-crash.sqlite3"
            clean = Path(directory) / "close-clean.sqlite3"
            run_json(db, "load", "--topology", str(TOPOLOGY))
            run_json(clean, "load", "--topology", str(TOPOLOGY))

            code, _ = run_cli(
                db,
                "ingest",
                "--events",
                str(EVENTS),
                env={"OBS_FAILPOINT": "ingest_after_window_close"},
                check=False,
            )
            self.assertEqual(code, 97)
            crashed = status(db)
            self.assertEqual(crashed["watermarks"], [])
            self.assertEqual(crashed["windows"], [])

            run_json(db, "resume")
            replayed = run_json(db, "ingest", "--events", str(EVENTS))
            self.assertEqual(replayed["processed_events"], 20)
            run_json(clean, "ingest", "--events", str(EVENTS))
            self.assertEqual(status_bytes(db), status_bytes(clean))
            self.assertEqual(
                window(db, "pay-2", "CPUThrottle", 0)["result"]["raw_event_count"], 4
            )

    def test_parallel_apply_and_ingest_are_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "parallel.sqlite3"
            run_json(db, "load", "--topology", str(TOPOLOGY))

            command = [
                sys.executable,
                "-m",
                "obs_workbench",
                "--db",
                str(db),
                "ingest",
                "--events",
                str(EVENTS),
            ]
            environment = os.environ.copy()
            environment.pop("OBS_FAILPOINT", None)
            first = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_out, first_err = first.communicate(timeout=30)
            second_out, second_err = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            processed = sorted(
                [json.loads(first_out)["processed_events"], json.loads(second_out)["processed_events"]]
            )
            self.assertEqual(processed, [0, 20])

            apply_command = [
                sys.executable,
                "-m",
                "obs_workbench",
                "--db",
                str(db),
                "apply",
                "--patch",
                str(PATCH),
            ]
            first = subprocess.Popen(
                apply_command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            second = subprocess.Popen(
                apply_command,
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            first_out, first_err = first.communicate(timeout=30)
            second_out, second_err = second.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, first_err)
            self.assertEqual(second.returncode, 0, second_err)
            idempotent = sorted(
                [json.loads(first_out)["idempotent"], json.loads(second_out)["idempotent"]]
            )
            self.assertEqual(idempotent, [False, True])
            self.assertEqual(status(db)["topology"]["published_topology_version"], 2)
            self.assertTrue(run_json(db, "verify")["ok"])


if __name__ == "__main__":
    unittest.main()
