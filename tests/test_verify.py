from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from verify import verify_session  # noqa: E402


def go_json_bytes(record: object) -> bytes:
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return encoded.encode("utf-8")


def write_journal(path: Path, records: list[dict]) -> None:
    with path.open("wb") as handle:
        for sequence, record in enumerate(records, 1):
            envelope = {
                "seq": sequence,
                "crc32": f"{zlib.crc32(go_json_bytes(record)) & 0xFFFFFFFF:08x}",
                "record": record,
            }
            handle.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")


def fixture() -> tuple[dict, dict, dict]:
    message = {
        "global_offset": 1,
        "version": 3,
        "delivery_id": "m1",
        "body": {"text": "fixture"},
        "timestamp_ms": 42,
        "retry_count": 0,
        "partition_key": "orders",
        "idempotency_seq": 9,
        "semantics": "at_least_once",
        "accepted_at": "2026-01-01T00:00:00Z",
    }
    delivery = {
        "delivery_id": "m1",
        "source_delivery_id": "m1",
        "consumer_id": "c1",
        "session_id": "s1",
        "consumer_version": 1,
        "consumer_offset": 1,
        "global_offset": 1,
        "attempt": 1,
        "payload": {"version": 1, "delivery_id": "m1", "body": {"text": "fixture"}},
        "conversion": {
            "conversion_id": "conv-1",
            "source_delivery_id": "m1",
            "delivery_id": "m1",
            "source_version": 3,
            "declared_version": 1,
            "direction": "downgrade",
            "source_fields": ["version", "delivery_id", "body", "timestamp_ms", "retry_count", "partition_key", "idempotency_seq"],
            "target_fields": ["version", "delivery_id", "body"],
            "stripped_fields": ["timestamp_ms", "retry_count", "partition_key", "idempotency_seq"],
            "filled_fields": [],
            "mutated_fields": [],
            "field_origins": {"version": "declared", "delivery_id": "source", "body": "source"},
        },
        "semantic": {
            "mode": "at_least_once",
            "decision": "deliver",
            "reason": "initial at_least_once delivery",
            "retry_count": 0,
        },
        "delivered_at": "2026-01-01T00:00:01Z",
    }
    ack = {
        "consumer_id": "c1",
        "delivery_id": "m1",
        "global_offset": 1,
        "consumer_offset": 1,
        "session_id": "s1",
        "semantics": "at_least_once",
        "acked_at": "2026-01-01T00:00:02Z",
    }
    return message, delivery, ack


class VerifyTests(unittest.TestCase):
    def create_fixture(self, temp: Path, delivery: dict) -> None:
        message, _, ack = fixture()
        write_journal(temp / "messages.log", [message])
        write_journal(temp / "deliveries.log", [delivery])
        write_journal(temp / "acks.log", [ack])
        write_journal(temp / "dedup.index", [])

    def test_valid_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, delivery, _ = fixture()
            self.create_fixture(data_dir, delivery)
            result = verify_session(data_dir)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["code"], "OK")
            self.assertEqual(result["summary"]["downgrades"], 1)

    def test_tampered_stripped_fields_reports_e_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _, delivery, _ = fixture()
            tampered = copy.deepcopy(delivery)
            tampered["conversion"]["stripped_fields"] = ["timestamp_ms"]
            self.create_fixture(data_dir, tampered)
            result = verify_session(data_dir)
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "E_VERIFY")
            self.assertTrue(any(error["field"] == "conversion.stripped_fields" for error in result["errors"]))
            match = next(error for error in result["errors"] if error["field"] == "conversion.stripped_fields")
            self.assertEqual(match["delivery_id"], "m1")
            self.assertIn("partition_key", match["expected"])

    def test_ack_offset_mismatch_reports_e_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            message, delivery, ack = fixture()
            bad_ack = copy.deepcopy(ack)
            bad_ack["consumer_offset"] = 7
            write_journal(data_dir / "messages.log", [message])
            write_journal(data_dir / "deliveries.log", [delivery])
            write_journal(data_dir / "acks.log", [bad_ack])
            write_journal(data_dir / "dedup.index", [])
            result = verify_session(data_dir)
            self.assertFalse(result["ok"])
            self.assertTrue(any(error["field"] == "ack.offset" for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
