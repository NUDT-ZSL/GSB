from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from delivery_gateway.errors import ErrorCode, GatewayError
from delivery_gateway.models import (
    parse_clock,
    parse_consumers,
    parse_quota,
    parse_topics,
)
from delivery_gateway.service import (
    confirm_command,
    deliver_command,
    load_gateway,
    resume_command,
    status_command,
    verify_command,
)

ROOT = Path(__file__).resolve().parents[1]


def deep_merge(base: dict, override: dict) -> dict:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        elif (
            isinstance(value, list)
            and isinstance(result.get(key), list)
            and len(value) == len(result[key])
            and all(isinstance(item, dict) for item in value + result[key])
        ):
            result[key] = [
                deep_merge(base_item, override_item)
                for base_item, override_item in zip(result[key], value)
            ]
        else:
            result[key] = value
    return result


DEFAULT_TOPICS = {
    "global_seed": 7,
    "topics": [
        {
            "name": "t",
            "partitions": 1,
            "partition_key_rule": "fnv1a32_mod",
            "backlog_limit": 4,
        }
    ],
}
DEFAULT_CONSUMERS = {
    "consumers": [
        {
            "consumer_id": "c1",
            "topic": "t",
            "request_size": 4,
            "rate_limit_per_sec": 10,
            "delivery_timeout_ms": 50,
        }
    ]
}
DEFAULT_QUOTA = {
    "config_version": 1,
    "global": {"capacity": 10, "refill_rate_per_sec": 10},
    "per_consumer": {"capacity": 10},
    "round_budget_ms": 200,
    "delivery_timeout_ms": 50,
    "max_delivery_generation": 3,
    "retry_backoff_base_ms": 10,
    "retry_backoff_max_ms": 20,
    "attempt_duration_ms": 5,
}
DEFAULT_CLOCK = {"initial_now_ms": 1000, "regression_tolerance_ms": 100}
DEFAULT_FAULTS = {"events": []}


class GatewayFixture:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.paths = {
            "topics": tmp / "topics.json",
            "consumers": tmp / "consumers.json",
            "quota": tmp / "quota.json",
            "clock": tmp / "clock.json",
            "faults": tmp / "faults.json",
        }
        self.db = tmp / "state.db"

    def write(
        self,
        *,
        topics: dict | None = None,
        consumers: dict | None = None,
        quota: dict | None = None,
        clock: dict | None = None,
        faults: dict | None = None,
    ) -> None:
        payloads = {
            "topics": topics or DEFAULT_TOPICS,
            "consumers": consumers or DEFAULT_CONSUMERS,
            "quota": quota or DEFAULT_QUOTA,
            "clock": clock or DEFAULT_CLOCK,
            "faults": faults or DEFAULT_FAULTS,
        }
        for name, payload in payloads.items():
            self.paths[name].write_text(
                json.dumps(payload, sort_keys=True), encoding="utf-8"
            )

    def load(self) -> dict:
        return load_gateway(
            self.db,
            topics_path=self.paths["topics"],
            consumers_path=self.paths["consumers"],
            quota_path=self.paths["quota"],
            clock_path=self.paths["clock"],
            faults_path=self.paths["faults"],
        )

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def scalar(self, sql: str, params: tuple = ()):
        rows = self.query(sql, params)
        if not rows:
            return None
        return rows[0][0]


def run_cli(
    args: list[str],
    *,
    env_extra: dict[str, str] | None = None,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "delivery_gateway.cli", *args],
        cwd=str(ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def cli_load_args(db: Path, paths: dict[str, Path]) -> list[str]:
    return [
        "load",
        "--db",
        str(db),
        "--topics",
        str(paths["topics"]),
        "--consumers",
        str(paths["consumers"]),
        "--quota",
        str(paths["quota"]),
        "--clock",
        str(paths["clock"]),
        "--faults",
        str(paths["faults"]),
    ]


class ValidationTests(unittest.TestCase):
    def test_distinct_validation_error_codes(self) -> None:
        cases: list[tuple[str, str]] = []
        duplicate_topics = {
            "global_seed": 1,
            "topics": [
                {"name": "t", "partitions": 1, "partition_key_rule": "fnv1a32_mod", "backlog_limit": 1},
                {"name": "t", "partitions": 1, "partition_key_rule": "fnv1a32_mod", "backlog_limit": 1},
            ],
        }
        invalid_partitions = deep_merge(DEFAULT_TOPICS, {"topics": [{"partitions": 0}]})
        invalid_rule = deep_merge(DEFAULT_TOPICS, {"topics": [{"partition_key_rule": "nope"}]})
        invalid_rate = deep_merge(DEFAULT_CONSUMERS, {"consumers": [{"rate_limit_per_sec": 0}]})
        invalid_timeout = deep_merge(DEFAULT_QUOTA, {"delivery_timeout_ms": 0})

        def capture(label: str, fn) -> None:
            try:
                fn()
            except GatewayError as exc:
                cases.append((label, exc.code))
            else:
                self.fail(f"{label} should fail validation")

        capture("duplicate_topic", lambda: parse_topics(duplicate_topics))
        capture("partitions", lambda: parse_topics(invalid_partitions))
        capture("rule", lambda: parse_topics(invalid_rule))
        topics = parse_topics(DEFAULT_TOPICS)
        capture(
            "duplicate_consumer",
            lambda: parse_consumers(
                {
                    "consumers": [
                        DEFAULT_CONSUMERS["consumers"][0],
                        DEFAULT_CONSUMERS["consumers"][0],
                    ]
                },
                topics,
            ),
        )
        capture("rate", lambda: parse_consumers(invalid_rate, topics))
        capture("timeout", lambda: parse_quota(invalid_timeout))
        capture(
            "capacity",
            lambda: parse_quota(deep_merge(DEFAULT_QUOTA, {"global": {"capacity": 0}})),
        )
        capture(
            "max_generation",
            lambda: parse_quota(deep_merge(DEFAULT_QUOTA, {"max_delivery_generation": -1})),
        )
        capture(
            "backoff",
            lambda: parse_quota(deep_merge(DEFAULT_QUOTA, {"retry_backoff_base_ms": 0})),
        )
        capture(
            "version",
            lambda: parse_quota(deep_merge(DEFAULT_QUOTA, {"config_version": 0})),
        )
        capture(
            "clock",
            lambda: parse_clock({"initial_now_ms": 0, "regression_tolerance_ms": -1}),
        )
        self.assertEqual(len(cases), len({code for _, code in cases}), cases)
        self.assertEqual(len(cases), 11)

    def test_invalid_load_does_not_create_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            duplicate_topics = {
                "global_seed": 1,
                "topics": [
                    {"name": "t", "partitions": 1, "partition_key_rule": "fnv1a32_mod", "backlog_limit": 1},
                    {"name": "t", "partitions": 1, "partition_key_rule": "fnv1a32_mod", "backlog_limit": 1},
                ],
            }
            fixture.write(topics=duplicate_topics)
            with self.assertRaises(GatewayError) as caught:
                fixture.load()
            self.assertEqual(caught.exception.code, ErrorCode.TOPIC_DUPLICATE)
            self.assertFalse(fixture.db.exists())

    def test_invalid_reload_does_not_mutate_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write()
            fixture.load()
            before = (
                fixture.scalar("SELECT value FROM meta WHERE key='queue_version'"),
                fixture.scalar("SELECT balance_scaled FROM buckets WHERE bucket_id='global'"),
                fixture.scalar("SELECT COUNT(*) FROM messages"),
            )
            fixture.write(
                quota=deep_merge(
                    DEFAULT_QUOTA,
                    {"global": {"capacity": 0}},
                )
            )
            with self.assertRaises(GatewayError) as caught:
                fixture.load()
            self.assertEqual(caught.exception.code, ErrorCode.GLOBAL_CAPACITY_INVALID)
            after = (
                fixture.scalar("SELECT value FROM meta WHERE key='queue_version'"),
                fixture.scalar("SELECT balance_scaled FROM buckets WHERE bucket_id='global'"),
                fixture.scalar("SELECT COUNT(*) FROM messages"),
            )
            self.assertEqual(before, after)

    def test_config_version_rollback_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                quota=deep_merge(DEFAULT_QUOTA, {"config_version": 3})
            )
            fixture.load()
            fixture.write(
                quota=deep_merge(DEFAULT_QUOTA, {"config_version": 2})
            )
            with self.assertRaises(GatewayError) as caught:
                fixture.load()
            self.assertEqual(caught.exception.code, ErrorCode.CONFIG_VERSION_ROLLBACK)
            fixture.write(
                quota=deep_merge(
                    DEFAULT_QUOTA,
                    {
                        "config_version": 3,
                        "attempt_duration_ms": 6,
                    },
                )
            )
            with self.assertRaises(GatewayError) as caught:
                fixture.load()
            self.assertEqual(caught.exception.code, ErrorCode.CONFIG_VERSION_CONFLICT)


class LedgerTests(unittest.TestCase):
    def _quota(self) -> dict:
        return deep_merge(
            DEFAULT_QUOTA,
            {
                "global": {"capacity": 10, "refill_rate_per_sec": 10},
                "per_consumer": {"capacity": 2},
                "round_budget_ms": 100,
            },
        )

    def _consumers(self) -> dict:
        return deep_merge(
            DEFAULT_CONSUMERS,
            {
                "consumers": [
                    {
                        "consumer_id": "c1",
                        "topic": "t",
                        "request_size": 4,
                        "rate_limit_per_sec": 1,
                        "delivery_timeout_ms": 50,
                    }
                ]
            },
        )

    def test_double_bucket_wait_and_bitwise_determinism(self) -> None:
        results = []
        statuses = []
        ledgers = []
        with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
            for tmp_name in (tmp_a, tmp_b):
                fixture = GatewayFixture(Path(tmp_name))
                fixture.write(quota=self._quota(), consumers=self._consumers())
                fixture.load()
                result = deliver_command(
                    fixture.db,
                    consumer_id="c1",
                    request_id="r1",
                    now_ms=1000,
                    limit=None,
                    message_id=None,
                    topic=None,
                )
                status = status_command(fixture.db)
                ledger = [
                    tuple(row)
                    for row in fixture.query(
                        "SELECT at_ms, global_before_scaled, global_refill_scaled, "
                        "global_after_scaled, consumer_before_scaled, consumer_refill_scaled, "
                        "consumer_after_scaled FROM ledger_events ORDER BY event_id"
                    )
                ]
                waits = [
                    tuple(row)
                    for row in fixture.query(
                        "SELECT at_ms, global_balance_scaled, consumer_balance_scaled, "
                        "needed_wait_ms, reason FROM token_waits ORDER BY wait_id"
                    )
                ]
                verification = verify_command(fixture.db)
                self.assertTrue(verification["ok"], verification)
                results.append(result)
                statuses.append(status)
                ledgers.append((ledger, waits))
        self.assertEqual(results[0], results[1])
        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual(ledgers[0], ledgers[1])
        self.assertEqual(results[0]["summary"]["delivered"], 2)
        self.assertEqual(results[0]["summary"]["not_started"], 1)
        self.assertEqual(results[0]["stop_reason"], "round_budget")
        self.assertEqual(results[0]["outcomes"][0]["generation"], 1)
        self.assertEqual(results[0]["outcomes"][1]["generation"], 1)
        self.assertEqual(results[0]["outcomes"][2]["reason"], "token_wait_exceeds_round_budget")
        consumer_status = statuses[0]["consumers"][0]
        self.assertEqual(consumer_status["token_balance_scaled"], 5_000)
        self.assertEqual(statuses[0]["global_bucket"]["token_balance_scaled"], 8_050_000)
        self.assertEqual(len(ledgers[0][0]), 2)
        self.assertEqual(len(ledgers[0][1]), 1)
        self.assertEqual(ledgers[0][1][0][3], 990)

    def test_round_budget_preserves_unstarted_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                quota=deep_merge(
                    DEFAULT_QUOTA,
                    {
                        "round_budget_ms": 100,
                        "attempt_duration_ms": 40,
                    },
                ),
                consumers=deep_merge(
                    DEFAULT_CONSUMERS,
                    {"consumers": [{"delivery_timeout_ms": 50}]},
                ),
            )
            fixture.load()
            result = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r-budget",
                now_ms=1000,
                limit=None,
                message_id=None,
                topic=None,
            )
            self.assertEqual(result["summary"]["delivered"], 2)
            self.assertEqual(result["summary"]["not_started"], 1)
            self.assertEqual(result["outcomes"][2]["reason"], "round_budget_exhausted")
            self.assertEqual(result["budget_used_ms"], 80)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 2)
            self.assertEqual(
                fixture.scalar("SELECT COUNT(*) FROM messages WHERE generation=0 AND state='ready'"),
                2,
            )
            self.assertTrue(verify_command(fixture.db)["ok"])


class IdempotencyTests(unittest.TestCase):
    def test_confirm_generation_and_cross_consumer_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                consumers=deep_merge(
                    DEFAULT_CONSUMERS,
                    {
                        "consumers": [
                            DEFAULT_CONSUMERS["consumers"][0],
                            {
                                "consumer_id": "c2",
                                "topic": "t",
                                "request_size": 1,
                                "rate_limit_per_sec": 1,
                                "delivery_timeout_ms": 50,
                            },
                        ]
                    },
                )
            )
            fixture.load()
            result = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r1",
                now_ms=1000,
                limit=2,
                message_id=None,
                topic=None,
            )
            first = result["outcomes"][0]
            delivery_id = first["delivery_id"]
            message_id = first["message_id"]
            confirmed = confirm_command(
                fixture.db,
                delivery_id=delivery_id,
                consumer_id="c1",
                now_ms=1010,
            )
            self.assertTrue(confirmed["confirmed"])
            self.assertFalse(confirmed["idempotent"])
            again = confirm_command(
                fixture.db,
                delivery_id=delivery_id,
                consumer_id="c1",
                now_ms=1010,
            )
            self.assertTrue(again["idempotent"])
            cross = confirm_command(
                fixture.db,
                delivery_id=delivery_id,
                consumer_id="c2",
                now_ms=1010,
            )
            self.assertFalse(cross["confirmed"])
            self.assertEqual(cross["ignored_reason"], "cross_consumer_confirmation")
            self.assertEqual(
                fixture.scalar(
                    "SELECT generation FROM messages WHERE message_id=?", (message_id,)
                ),
                1,
            )
            self.assertEqual(
                fixture.scalar(
                    "SELECT state FROM messages WHERE message_id=?", (message_id,)
                ),
                "confirmed",
            )
            repeated = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r-specific",
                now_ms=1010,
                limit=1,
                message_id=message_id,
                topic=None,
            )
            self.assertEqual(repeated["outcomes"][0]["reason"], "already_confirmed")
            self.assertEqual(
                fixture.scalar(
                    "SELECT generation FROM messages WHERE message_id=?", (message_id,)
                ),
                1,
            )
            self.assertTrue(verify_command(fixture.db)["ok"])

    def test_request_retry_is_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write()
            fixture.load()
            args = [
                "deliver",
                "--db",
                str(fixture.db),
                "--consumer",
                "c1",
                "--request-id",
                "same",
                "--now-ms",
                "1000",
            ]
            first = run_cli(args)
            second = run_cli(args)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 4)


class FailureTests(unittest.TestCase):
    def test_backoff_then_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                topics=deep_merge(
                    DEFAULT_TOPICS,
                    {
                        "topics": [
                            {
                                "name": "t",
                                "partitions": 1,
                                "partition_key_rule": "fnv1a32_mod",
                                "backlog_limit": 2,
                            }
                        ]
                    },
                ),
                consumers=deep_merge(
                    DEFAULT_CONSUMERS,
                    {
                        "consumers": [
                            {
                                "consumer_id": "c1",
                                "topic": "t",
                                "request_size": 1,
                                "rate_limit_per_sec": 10,
                                "delivery_timeout_ms": 50,
                            }
                        ]
                    },
                ),
                quota=deep_merge(
                    DEFAULT_QUOTA,
                    {"max_delivery_generation": 2},
                ),
                faults={
                    "events": [
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 0,
                            "generation": 1,
                            "action": "fail",
                        },
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 0,
                            "generation": 2,
                            "action": "fail",
                        },
                    ]
                },
            )
            fixture.load()
            first = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="f1",
                now_ms=1000,
                limit=1,
                message_id=None,
                topic=None,
            )
            self.assertEqual(first["outcomes"][0]["status"], "failed")
            retry = fixture.query("SELECT * FROM retry_events")
            self.assertEqual(len(retry), 1)
            self.assertGreaterEqual(int(retry[0]["delay_ms"]), 10)
            self.assertLessEqual(int(retry[0]["delay_ms"]), 20)
            second = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="f2",
                now_ms=2000,
                limit=1,
                message_id=None,
                topic=None,
            )
            self.assertEqual(second["outcomes"][0]["status"], "failed")
            self.assertEqual(second["outcomes"][0]["generation"], 2)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM retry_events"), 1)
            dead = fixture.query("SELECT * FROM dead_letters")
            self.assertEqual(len(dead), 1)
            self.assertEqual(str(dead[0]["reason"]), "failed")
            self.assertEqual(
                fixture.scalar("SELECT state FROM messages WHERE message_id=?", (dead[0]["message_id"],)),
                "dead_letter",
            )
            self.assertTrue(verify_command(fixture.db)["ok"])

    def test_reject_expire_and_proactive_abandonment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                topics=deep_merge(
                    DEFAULT_TOPICS,
                    {
                        "topics": [
                            {
                                "name": "t",
                                "partitions": 1,
                                "partition_key_rule": "fnv1a32_mod",
                                "backlog_limit": 3,
                            }
                        ]
                    },
                ),
                consumers=deep_merge(
                    DEFAULT_CONSUMERS,
                    {
                        "consumers": [
                            {
                                "consumer_id": "c1",
                                "topic": "t",
                                "request_size": 3,
                                "rate_limit_per_sec": 10,
                                "delivery_timeout_ms": 50,
                            }
                        ]
                    },
                ),
                faults={
                    "events": [
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 0,
                            "generation": 1,
                            "action": "reject",
                        },
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 1,
                            "generation": 1,
                            "action": "expire",
                        },
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 2,
                            "generation": 1,
                            "action": "slow",
                            "duration_ms": 60,
                        },
                    ]
                },
            )
            fixture.load()
            result = deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="mixed",
                now_ms=1000,
                limit=3,
                message_id=None,
                topic=None,
            )
            self.assertEqual(result["summary"]["failed_in_flight"], 2)
            self.assertEqual(result["summary"]["abandoned"], 1)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM ledger_events"), 2)
            reasons = {
                str(row["reason"]) for row in fixture.query("SELECT reason FROM dead_letters")
            }
            self.assertEqual(reasons, {"rejected", "expired"})
            self.assertEqual(
                fixture.scalar("SELECT COUNT(*) FROM messages WHERE generation=0 AND state='ready'"),
                1,
            )
            self.assertTrue(verify_command(fixture.db)["ok"])

    def test_verify_detects_bucket_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write()
            fixture.load()
            deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r1",
                now_ms=1000,
                limit=1,
                message_id=None,
                topic=None,
            )
            conn = sqlite3.connect(fixture.db)
            try:
                conn.execute("UPDATE buckets SET balance_scaled=balance_scaled+1 WHERE bucket_id='global'")
                conn.commit()
            finally:
                conn.close()
            result = verify_command(fixture.db)
            self.assertFalse(result["ok"])
            self.assertTrue(any(error["check"] == "bucket_final_balance" for error in result["errors"]))


class CrashRecoveryTests(unittest.TestCase):
    def _fixture(self, tmp_name: str, *, backlog: int = 1) -> GatewayFixture:
        fixture = GatewayFixture(Path(tmp_name))
        fixture.write(
            topics=deep_merge(
                DEFAULT_TOPICS,
                {
                    "topics": [
                        {
                            "name": "t",
                            "partitions": 1,
                            "partition_key_rule": "fnv1a32_mod",
                            "backlog_limit": backlog,
                        }
                    ]
                },
            ),
            quota=deep_merge(DEFAULT_QUOTA, {"max_delivery_generation": 3}),
        )
        fixture.load()
        return fixture

    def _deliver_args(self, fixture: GatewayFixture) -> list[str]:
        return [
            "deliver",
            "--db",
            str(fixture.db),
            "--consumer",
            "c1",
            "--request-id",
            "crash",
            "--now-ms",
            "1000",
            "--limit",
            "1",
        ]

    def _assert_recovered_once(self, fixture: GatewayFixture) -> None:
        self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM ledger_events"), 1)
        self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 1)
        self.assertEqual(fixture.scalar("SELECT MAX(generation) FROM messages"), 1)
        self.assertEqual(
            fixture.scalar(
                "SELECT tokens_spent_scaled FROM consumer_usage WHERE consumer_id='c1'"
            ),
            1_000_000,
        )
        verification = verify_command(fixture.db)
        self.assertTrue(verification["ok"], verification)

    def test_sigkill_before_claim_commit_replays_whole_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = self._fixture(tmp_name)
            proc = run_cli(
                self._deliver_args(fixture),
                env_extra={"GATEWAY_CRASH_POINT": "claim_before_commit"},
            )
            self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 0)
            self.assertEqual(
                fixture.scalar("SELECT balance_scaled FROM buckets WHERE bucket_id='global'"),
                10_000_000,
            )
            resumed = resume_command(fixture.db, now_ms=1000)
            self.assertEqual(resumed["resumed_deliveries"], 0)
            self.assertEqual(resumed["completed_requests"], 1)
            self._assert_recovered_once(fixture)

    def test_sigkill_after_bucket_or_generation_update_rolls_back(self) -> None:
        for crash_point in (
            "claim_after_bucket_update",
            "claim_after_generation_increment",
        ):
            with self.subTest(crash_point=crash_point), tempfile.TemporaryDirectory() as tmp_name:
                fixture = self._fixture(tmp_name)
                proc = run_cli(
                    self._deliver_args(fixture),
                    env_extra={"GATEWAY_CRASH_POINT": crash_point},
                )
                self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
                self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 0)
                self.assertEqual(
                    fixture.scalar(
                        "SELECT balance_scaled FROM buckets WHERE bucket_id='global'"
                    ),
                    10_000_000,
                )
                resume_command(fixture.db, now_ms=1000)
                self._assert_recovered_once(fixture)

    def test_sigkill_after_claim_commit_does_not_recharge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = self._fixture(tmp_name)
            proc = run_cli(
                self._deliver_args(fixture),
                env_extra={"GATEWAY_CRASH_POINT": "claim_after_commit"},
            )
            self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 1)
            self.assertEqual(fixture.scalar("SELECT status FROM deliveries"), "started")
            charged = fixture.scalar(
                "SELECT balance_scaled FROM buckets WHERE bucket_id='global'"
            )
            resumed = resume_command(fixture.db, now_ms=1000)
            self.assertEqual(resumed["resumed_deliveries"], 1)
            self.assertEqual(
                fixture.scalar("SELECT balance_scaled FROM buckets WHERE bucket_id='global'"),
                charged,
            )
            self._assert_recovered_once(fixture)

    def test_sigkill_after_tombstone_rolls_back_dead_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write(
                topics=deep_merge(
                    DEFAULT_TOPICS,
                    {
                        "topics": [
                            {
                                "name": "t",
                                "partitions": 1,
                                "partition_key_rule": "fnv1a32_mod",
                                "backlog_limit": 1,
                            }
                        ]
                    },
                ),
                faults={
                    "events": [
                        {
                            "consumer_id": "c1",
                            "topic": "t",
                            "partition": 0,
                            "seq": 0,
                            "generation": 1,
                            "action": "expire",
                        }
                    ]
                },
            )
            fixture.load()
            proc = run_cli(
                self._deliver_args(fixture),
                env_extra={"GATEWAY_CRASH_POINT": "complete_after_tombstone"},
            )
            self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM dead_letters"), 0)
            self.assertEqual(fixture.scalar("SELECT status FROM deliveries"), "started")
            self.assertEqual(
                fixture.scalar("SELECT state FROM messages LIMIT 1"),
                "inflight",
            )
            resumed = resume_command(fixture.db, now_ms=1000)
            self.assertEqual(resumed["resumed_deliveries"], 1)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM dead_letters"), 1)
            self.assertEqual(
                fixture.scalar("SELECT state FROM messages LIMIT 1"),
                "dead_letter",
            )
            self.assertTrue(verify_command(fixture.db)["ok"])

    def test_sigkill_before_completion_commit_replays_completion_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = self._fixture(tmp_name)
            proc = run_cli(
                self._deliver_args(fixture),
                env_extra={"GATEWAY_CRASH_POINT": "complete_before_commit"},
            )
            self.assertEqual(proc.returncode, -9, proc.stdout + proc.stderr)
            self.assertEqual(fixture.scalar("SELECT status FROM deliveries"), "started")
            resumed = resume_command(fixture.db, now_ms=1000)
            self.assertEqual(resumed["resumed_deliveries"], 1)
            self._assert_recovered_once(fixture)


class ConcurrencyTests(unittest.TestCase):
    def _fixture(self, tmp_name: str) -> GatewayFixture:
        fixture = GatewayFixture(Path(tmp_name))
        fixture.write(
            topics=deep_merge(
                DEFAULT_TOPICS,
                {
                    "topics": [
                        {
                            "name": "t",
                            "partitions": 1,
                            "partition_key_rule": "fnv1a32_mod",
                            "backlog_limit": 10,
                        }
                    ]
                },
            ),
            consumers={
                "consumers": [
                    {
                        "consumer_id": "c1",
                        "topic": "t",
                        "request_size": 5,
                        "rate_limit_per_sec": 10,
                        "delivery_timeout_ms": 50,
                    },
                    {
                        "consumer_id": "c2",
                        "topic": "t",
                        "request_size": 5,
                        "rate_limit_per_sec": 10,
                        "delivery_timeout_ms": 50,
                    },
                ]
            },
            quota=deep_merge(
                DEFAULT_QUOTA,
                {
                    "global": {"capacity": 5, "refill_rate_per_sec": 1},
                    "per_consumer": {"capacity": 5},
                    "round_budget_ms": 100,
                    "attempt_duration_ms": 5,
                },
            ),
        )
        fixture.load()
        return fixture

    def test_two_gateways_and_resume_do_not_oversell(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = self._fixture(tmp_name)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            commands = [
                [
                    sys.executable,
                    "-m",
                    "delivery_gateway.cli",
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "c1",
                    "--request-id",
                    "p1",
                    "--now-ms",
                    "1000",
                ],
                [
                    sys.executable,
                    "-m",
                    "delivery_gateway.cli",
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "c2",
                    "--request-id",
                    "p2",
                    "--now-ms",
                    "1000",
                ],
                [
                    sys.executable,
                    "-m",
                    "delivery_gateway.cli",
                    "resume",
                    "--db",
                    str(fixture.db),
                    "--now-ms",
                    "1000",
                ],
            ]
            procs = [
                subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for command in commands
            ]
            outputs = [proc.communicate(timeout=30) for proc in procs]
            for proc, (stdout, stderr) in zip(procs, outputs):
                self.assertEqual(proc.returncode, 0, stdout + stderr)
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 5)
            self.assertEqual(
                fixture.scalar("SELECT COUNT(*) FROM (SELECT message_id, generation, COUNT(*) c "
                               "FROM deliveries GROUP BY message_id, generation HAVING c>1)"),
                0,
            )
            self.assertEqual(
                fixture.scalar(
                    "SELECT SUM(charge_scaled) FROM ledger_events WHERE delivery_id IN "
                    "(SELECT delivery_id FROM ledger_events)"
                ),
                5_000_000,
            )
            global_balance = fixture.scalar(
                "SELECT balance_scaled FROM buckets WHERE bucket_id='global'"
            )
            self.assertGreaterEqual(global_balance, 0)
            self.assertLess(global_balance, 1_000_000)
            verification = verify_command(fixture.db)
            self.assertTrue(verification["ok"], verification)

    def test_concurrent_same_request_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = self._fixture(tmp_name)
            env = os.environ.copy()
            env["PYTHONPATH"] = str(ROOT / "src")
            command = [
                sys.executable,
                "-m",
                "delivery_gateway.cli",
                "deliver",
                "--db",
                str(fixture.db),
                "--consumer",
                "c1",
                "--request-id",
                "same-request",
                "--now-ms",
                "1000",
            ]
            procs = [
                subprocess.Popen(
                    command,
                    cwd=str(ROOT),
                    env=env,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for _ in range(2)
            ]
            outputs = [proc.communicate(timeout=30) for proc in procs]
            for proc, (stdout, stderr) in zip(procs, outputs):
                self.assertEqual(proc.returncode, 0, stdout + stderr)
            self.assertEqual(outputs[0][0], outputs[1][0])
            self.assertEqual(fixture.scalar("SELECT COUNT(*) FROM deliveries"), 5)
            self.assertTrue(verify_command(fixture.db)["ok"])


class ErrorMappingTests(unittest.TestCase):
    def test_cli_error_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write()
            fixture.load()
            missing_consumer = run_cli(
                [
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "missing",
                    "--request-id",
                    "x",
                    "--now-ms",
                    "1000",
                ]
            )
            self.assertEqual(missing_consumer.returncode, 2)
            self.assertEqual(
                json.loads(missing_consumer.stdout)["error"]["code"],
                ErrorCode.CONSUMER_NOT_FOUND,
            )
            missing_topic = run_cli(
                [
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "c1",
                    "--topic",
                    "missing",
                    "--request-id",
                    "x",
                    "--now-ms",
                    "1000",
                ]
            )
            self.assertEqual(
                json.loads(missing_topic.stdout)["error"]["code"],
                ErrorCode.TOPIC_NOT_FOUND,
            )
            missing_message = run_cli(
                [
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "c1",
                    "--message-id",
                    "msg_missing",
                    "--request-id",
                    "x",
                    "--now-ms",
                    "1000",
                ]
            )
            self.assertEqual(
                json.loads(missing_message.stdout)["error"]["code"],
                ErrorCode.MESSAGE_NOT_FOUND,
            )
            missing_delivery = run_cli(
                [
                    "confirm",
                    "--db",
                    str(fixture.db),
                    "--delivery-id",
                    "dlv_missing",
                    "--now-ms",
                    "1000",
                ]
            )
            self.assertEqual(
                json.loads(missing_delivery.stdout)["error"]["code"],
                ErrorCode.DELIVERY_NOT_FOUND,
            )
            deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r1",
                now_ms=1000,
                limit=1,
                message_id=None,
                topic=None,
            )
            regression = run_cli(
                [
                    "deliver",
                    "--db",
                    str(fixture.db),
                    "--consumer",
                    "c1",
                    "--request-id",
                    "r2",
                    "--now-ms",
                    "900",
                ]
            )
            self.assertEqual(
                json.loads(regression.stdout)["error"]["code"],
                ErrorCode.CLOCK_REGRESSION,
            )

    def test_status_output_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            fixture = GatewayFixture(Path(tmp_name))
            fixture.write()
            fixture.load()
            deliver_command(
                fixture.db,
                consumer_id="c1",
                request_id="r1",
                now_ms=1000,
                limit=1,
                message_id=None,
                topic=None,
            )
            first = run_cli(["status", "--db", str(fixture.db)])
            second = run_cli(["status", "--db", str(fixture.db)])
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(first.stdout, second.stdout)


if __name__ == "__main__":
    unittest.main()
