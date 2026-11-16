#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

FIELDS = {
    1: ["version", "delivery_id", "body"],
    2: ["version", "delivery_id", "body", "timestamp_ms", "retry_count"],
    3: ["version", "delivery_id", "body", "timestamp_ms", "retry_count", "partition_key", "idempotency_seq"],
}
SEMANTICS = {"at_most_once", "at_least_once", "exactly_once"}
JOURNAL_NAMES = {
    "messages": "messages.log",
    "deliveries": "deliveries.log",
    "acks": "acks.log",
    "dedup": "dedup.index",
}


class Verification:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    def fail(
        self,
        delivery_id: str,
        field: str,
        expected: Any,
        actual: Any,
        message: str,
    ) -> None:
        self.errors.append(
            {
                "code": "E_VERIFY",
                "delivery_id": delivery_id,
                "field": field,
                "expected": expected,
                "actual": actual,
                "message": message,
            }
        )


def go_json_bytes(record: Any) -> bytes:
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return encoded.encode("utf-8")


def read_journal(path: Path, verification: Verification) -> list[tuple[int, dict[str, Any]]]:
    if not path.exists():
        return []
    records: list[tuple[int, dict[str, Any]]] = []
    expected_seq = 1
    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                envelope = json.loads(raw_line)
                record = envelope["record"]
                seq = int(envelope["seq"])
                crc = str(envelope["crc32"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                verification.fail(
                    "",
                    f"{path.name}.line",
                    "valid CRC-framed JSON",
                    raw_line[:120].decode("utf-8", "replace"),
                    f"invalid journal frame at line {line_number}: {exc}",
                )
                break
            computed = f"{zlib.crc32(go_json_bytes(record)) & 0xFFFFFFFF:08x}"
            if seq != expected_seq:
                verification.fail("", f"{path.name}.seq", expected_seq, seq, "journal sequence is not monotonic")
            if crc != computed:
                verification.fail("", f"{path.name}.crc32", computed, crc, "journal frame CRC mismatch")
            records.append((seq, record))
            expected_seq = seq + 1
    return records


def sorted_diff(left: list[str], right: list[str]) -> list[str]:
    right_set = set(right)
    return sorted(item for item in left if item not in right_set)


def unique_sorted(values: list[str]) -> list[str]:
    return sorted(set(values))


def expected_origins(message: dict[str, Any], target_version: int, retry_count: int) -> dict[str, str]:
    source_version = int(message["version"])
    origins = {"version": "declared", "delivery_id": "source", "body": "source"}
    if target_version >= 2:
        origins["timestamp_ms"] = "source" if source_version >= 2 else "default"
        if source_version < 2:
            origins["retry_count"] = "default" if retry_count == 0 else "derived"
        elif retry_count != message.get("retry_count", 0):
            origins["retry_count"] = "mutated"
        else:
            origins["retry_count"] = "source"
    if target_version == 3:
        origins["partition_key"] = "source" if source_version == 3 else "default"
        origins["idempotency_seq"] = "source" if source_version == 3 else "default"
    return origins


def canonical_body(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def verify_session(data_dir: Path) -> dict[str, Any]:
    verification = Verification()
    journals = {name: read_journal(data_dir / filename, verification) for name, filename in JOURNAL_NAMES.items()}
    messages: dict[int, dict[str, Any]] = {}
    exact_sequences: dict[int, str] = {}
    messages_by_delivery_id: dict[str, dict[str, Any]] = {}

    for envelope_seq, message in journals["messages"]:
        delivery_id = str(message.get("delivery_id", ""))
        version = message.get("version")
        semantics = message.get("semantics")
        if version not in FIELDS:
            verification.fail(delivery_id, "message.version", sorted(FIELDS), version, "unsupported source version")
            continue
        if int(message.get("global_offset", -1)) != envelope_seq:
            verification.fail(
                delivery_id,
                "message.global_offset",
                envelope_seq,
                message.get("global_offset"),
                "message offset must equal its durable journal sequence",
            )
        if semantics not in SEMANTICS:
            verification.fail(delivery_id, "message.semantics", sorted(SEMANTICS), semantics, "invalid semantics")
        if not delivery_id:
            verification.fail(delivery_id, "message.delivery_id", "non-empty string", delivery_id, "missing delivery id")
        if delivery_id in messages_by_delivery_id:
            verification.fail(delivery_id, "message.delivery_id", "unique", "duplicate", "message ids must be unique")
        messages[envelope_seq] = message
        messages_by_delivery_id[delivery_id] = message
        if semantics == "exactly_once":
            sequence = message.get("idempotency_seq")
            if version != 3 or not isinstance(sequence, int) or sequence <= 0:
                verification.fail(delivery_id, "message.idempotency_seq", "positive v3 integer", sequence, "invalid exactly-once key")
            elif sequence in exact_sequences:
                verification.fail(
                    delivery_id,
                    "message.idempotency_seq",
                    f"unique (already used by {exact_sequences[sequence]})",
                    sequence,
                    "a duplicate exactly-once sequence reached the durable message log",
                )
            else:
                exact_sequences[sequence] = delivery_id

    dedup_sequences: Counter[int] = Counter()
    for _, dedup in journals["dedup"]:
        sequence = dedup.get("idempotency_seq")
        if isinstance(sequence, int):
            dedup_sequences[sequence] += 1
        message = messages.get(int(dedup.get("global_offset", 0)))
        if not message:
            verification.fail(str(dedup.get("delivery_id", "")), "dedup.global_offset", "known message", dedup.get("global_offset"), "dedup entry points to missing message")
            continue
        if dedup.get("delivery_id") != message.get("delivery_id"):
            verification.fail(str(dedup.get("delivery_id", "")), "dedup.delivery_id", message.get("delivery_id"), dedup.get("delivery_id"), "dedup delivery id differs from message")
        if message.get("semantics") != "exactly_once":
            verification.fail(str(dedup.get("delivery_id", "")), "dedup.semantics", "exactly_once", message.get("semantics"), "dedup index contains a non-exactly-once message")
    for sequence, delivery_id in exact_sequences.items():
        count = dedup_sequences.get(sequence, 0)
        if count != 1:
            verification.fail(delivery_id, "dedup.idempotency_seq", 1, count, "exactly-once key must have exactly one durable index entry")
    for sequence, count in dedup_sequences.items():
        if count > 1:
            verification.fail("", "dedup.idempotency_seq", 1, count, f"idempotency sequence {sequence} has duplicate index entries")
        if sequence not in exact_sequences:
            verification.fail("", "dedup.idempotency_seq", "known exactly-once message", sequence, "orphan dedup index entry")

    deliveries: list[tuple[int, dict[str, Any]]] = journals["deliveries"]
    acks: list[tuple[int, dict[str, Any]]] = journals["acks"]
    delivery_by_consumer_id: dict[tuple[str, str], dict[str, Any]] = {}
    first_offsets: dict[str, list[int]] = defaultdict(list)
    attempts: dict[tuple[str, int], list[int]] = defaultdict(list)
    exact_offsets: dict[tuple[str, int], set[int]] = defaultdict(set)
    conversion_counts: Counter[str] = Counter()
    semantic_counts: Counter[str] = Counter()
    stripped_instances = 0
    filled_instances = 0

    for event_seq, delivery in deliveries:
        delivery_id = str(delivery.get("delivery_id", ""))
        consumer_id = str(delivery.get("consumer_id", ""))
        source_offset = delivery.get("global_offset")
        source = messages.get(int(source_offset)) if isinstance(source_offset, int) else None
        if source is None:
            verification.fail(delivery_id, "delivery.global_offset", "known source message", source_offset, "delivery is not traceable to a source message")
            continue
        source_version = int(source["version"])
        target_version = delivery.get("consumer_version")
        conversion = delivery.get("conversion", {})
        semantic = delivery.get("semantic", {})
        payload = delivery.get("payload", {})
        attempt = int(delivery.get("attempt", 0))
        consumer_offset = delivery.get("consumer_offset")
        if target_version not in FIELDS:
            verification.fail(delivery_id, "delivery.consumer_version", sorted(FIELDS), target_version, "unsupported declared consumer version")
            continue
        if delivery.get("source_delivery_id") != source.get("delivery_id"):
            verification.fail(delivery_id, "delivery.source_delivery_id", source.get("delivery_id"), delivery.get("source_delivery_id"), "source traceability mismatch")
        if delivery.get("delivery_id") != source.get("delivery_id"):
            verification.fail(delivery_id, "delivery.delivery_id", source.get("delivery_id"), delivery.get("delivery_id"), "delivery id changed during conversion")
        if conversion.get("source_version") != source_version:
            verification.fail(delivery_id, "conversion.source_version", source_version, conversion.get("source_version"), "source version mismatch")
        if conversion.get("declared_version") != target_version:
            verification.fail(delivery_id, "conversion.declared_version", target_version, conversion.get("declared_version"), "declared version mismatch")
        expected_direction = "downgrade" if target_version < source_version else "upcast" if target_version > source_version else "same"
        if conversion.get("direction") != expected_direction:
            verification.fail(delivery_id, "conversion.direction", expected_direction, conversion.get("direction"), "conversion direction is inconsistent with versions")
        source_fields = FIELDS[source_version]
        target_fields = FIELDS[target_version]
        expected_stripped = sorted_diff(source_fields, target_fields)
        expected_filled = sorted_diff(target_fields, source_fields)
        if unique_sorted(conversion.get("source_fields", [])) != sorted(source_fields) or len(conversion.get("source_fields", [])) != len(source_fields):
            verification.fail(delivery_id, "conversion.source_fields", sorted(source_fields), conversion.get("source_fields"), "source field set does not match source version")
        if unique_sorted(conversion.get("target_fields", [])) != sorted(target_fields) or len(conversion.get("target_fields", [])) != len(target_fields):
            verification.fail(delivery_id, "conversion.target_fields", sorted(target_fields), conversion.get("target_fields"), "target field set does not match declared version")
        if unique_sorted(conversion.get("stripped_fields", [])) != expected_stripped or len(conversion.get("stripped_fields", [])) != len(expected_stripped):
            verification.fail(delivery_id, "conversion.stripped_fields", expected_stripped, conversion.get("stripped_fields"), "downgrade stripped fields do not match subscriber capability")
        if unique_sorted(conversion.get("filled_fields", [])) != expected_filled or len(conversion.get("filled_fields", [])) != len(expected_filled):
            verification.fail(delivery_id, "conversion.filled_fields", expected_filled, conversion.get("filled_fields"), "upcast default fields do not match subscriber capability")
        stripped_instances += len(expected_stripped)
        filled_instances += len(expected_filled)
        actual_payload_fields = list(payload.keys())
        if unique_sorted(actual_payload_fields) != sorted(target_fields) or len(actual_payload_fields) != len(target_fields):
            verification.fail(delivery_id, "payload.fields", sorted(target_fields), sorted(actual_payload_fields), "payload fields do not exactly match declared version")
        if payload.get("version") != target_version:
            verification.fail(delivery_id, "payload.version", target_version, payload.get("version"), "payload version must equal declared version")
        if payload.get("delivery_id") != source.get("delivery_id"):
            verification.fail(delivery_id, "payload.delivery_id", source.get("delivery_id"), payload.get("delivery_id"), "payload delivery id mismatch")
        if canonical_body(payload.get("body")) != canonical_body(source.get("body")):
            verification.fail(delivery_id, "payload.body", source.get("body"), payload.get("body"), "body changed during conversion")
        if target_version >= 2 and payload.get("timestamp_ms") != source.get("timestamp_ms", 0):
            verification.fail(delivery_id, "payload.timestamp_ms", source.get("timestamp_ms", 0), payload.get("timestamp_ms"), "timestamp was not preserved or defaulted correctly")
        if target_version == 3:
            expected_partition = source.get("partition_key") if source_version == 3 else "_default"
            expected_sequence = source.get("idempotency_seq", 0) if source_version == 3 else 0
            if payload.get("partition_key") != expected_partition:
                verification.fail(delivery_id, "payload.partition_key", expected_partition, payload.get("partition_key"), "partition key was not preserved or defaulted correctly")
            if payload.get("idempotency_seq") != expected_sequence:
                verification.fail(delivery_id, "payload.idempotency_seq", expected_sequence, payload.get("idempotency_seq"), "idempotency sequence was not preserved or defaulted correctly")
        expected_retry = int(source.get("retry_count", 0))
        if source.get("semantics") == "at_least_once":
            expected_retry += attempt - 1
        if target_version >= 2 and payload.get("retry_count") != expected_retry:
            verification.fail(delivery_id, "payload.retry_count", expected_retry, payload.get("retry_count"), "retry count does not match semantic attempt")
        expected_mutated = []
        if target_version >= 2 and source_version >= 2 and expected_retry != source.get("retry_count", 0):
            expected_mutated = ["retry_count"]
        if unique_sorted(conversion.get("mutated_fields", [])) != expected_mutated or len(conversion.get("mutated_fields", [])) != len(expected_mutated):
            verification.fail(delivery_id, "conversion.mutated_fields", expected_mutated, conversion.get("mutated_fields"), "mutated field metadata is inconsistent")
        expected_origin_map = expected_origins(source, target_version, expected_retry)
        if conversion.get("field_origins") != expected_origin_map:
            verification.fail(delivery_id, "conversion.field_origins", expected_origin_map, conversion.get("field_origins"), "field origins do not distinguish source and default values")
        if semantic.get("mode") != source.get("semantics"):
            verification.fail(delivery_id, "semantic.mode", source.get("semantics"), semantic.get("mode"), "semantic mode changed during conversion")
        expected_decision = {
            "at_most_once": "deliver_once",
            "at_least_once": "deliver" if attempt == 1 else "redeliver_unacked",
            "exactly_once": "deliver_exactly_once" if attempt == 1 else "redeliver_exactly_once_same_result",
        }.get(str(source.get("semantics")))
        if semantic.get("decision") != expected_decision:
            verification.fail(delivery_id, "semantic.decision", expected_decision, semantic.get("decision"), "semantic decision is inconsistent with mode and attempt")
        if semantic.get("retry_count") != expected_retry:
            verification.fail(delivery_id, "semantic.retry_count", expected_retry, semantic.get("retry_count"), "semantic retry count is inconsistent")
        if source.get("semantics") == "exactly_once":
            expected_key = f"idem:{source.get('idempotency_seq')}"
            if semantic.get("idempotency_seq") != source.get("idempotency_seq"):
                verification.fail(delivery_id, "semantic.idempotency_seq", source.get("idempotency_seq"), semantic.get("idempotency_seq"), "exactly-once key was lost after conversion")
            if semantic.get("dedup_key") != expected_key:
                verification.fail(delivery_id, "semantic.dedup_key", expected_key, semantic.get("dedup_key"), "dedup key was lost after conversion")
        if not isinstance(consumer_offset, int) or consumer_offset < 1:
            verification.fail(delivery_id, "delivery.consumer_offset", "positive integer", consumer_offset, "invalid consumer offset")
        delivery_by_consumer_id[(consumer_id, delivery_id)] = delivery
        attempts[(consumer_id, int(source_offset))].append(attempt)
        if consumer_id and isinstance(consumer_offset, int):
            if (consumer_id, int(source_offset)) not in exact_offsets:
                first_offsets[consumer_id].append(consumer_offset)
            else:
                if consumer_offset not in exact_offsets[(consumer_id, int(source_offset))]:
                    verification.fail(delivery_id, "delivery.consumer_offset", sorted(exact_offsets[(consumer_id, int(source_offset))]), consumer_offset, "retry changed the consumer offset")
            exact_offsets[(consumer_id, int(source_offset))].add(consumer_offset)
        conversion_counts[expected_direction] += 1
        semantic_counts[str(source.get("semantics"))] += 1

    for (consumer_id, source_offset), attempt_values in attempts.items():
        expected_attempts = list(range(1, len(attempt_values) + 1))
        if attempt_values != expected_attempts:
            source = messages.get(source_offset, {})
            verification.fail(str(source.get("delivery_id", "")), "delivery.attempt", expected_attempts, attempt_values, f"attempt sequence for consumer {consumer_id} is not contiguous")
        source = messages.get(source_offset, {})
        if source.get("semantics") == "at_most_once" and len(attempt_values) != 1:
            verification.fail(str(source.get("delivery_id", "")), "semantic.at_most_once", 1, len(attempt_values), f"consumer {consumer_id} received an at-most-once message more than once")

    unique_offsets_by_consumer: dict[str, list[int]] = defaultdict(list)
    for consumer_id, offsets in first_offsets.items():
        for offset in offsets:
            if offset not in unique_offsets_by_consumer[consumer_id]:
                unique_offsets_by_consumer[consumer_id].append(offset)
    for consumer_id, offsets in unique_offsets_by_consumer.items():
        expected = list(range(1, len(offsets) + 1))
        if offsets != expected:
            verification.fail("", "consumer.offset", expected, offsets, f"consumer {consumer_id} offsets are not monotonic and contiguous")

    ack_keys: Counter[tuple[str, str]] = Counter()
    ack_event_sequence: dict[tuple[str, str], int] = {}
    deliveries_by_key: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for event_seq, delivery in deliveries:
        deliveries_by_key[(str(delivery.get("consumer_id", "")), str(delivery.get("delivery_id", "")))].append(
            (event_seq, str(delivery.get("delivered_at", "")))
        )
    for ack_seq, ack in acks:
        consumer_id = str(ack.get("consumer_id", ""))
        delivery_id = str(ack.get("delivery_id", ""))
        key = (consumer_id, delivery_id)
        ack_keys[key] += 1
        ack_event_sequence[key] = ack_seq
        delivery = delivery_by_consumer_id.get(key)
        if delivery is None:
            verification.fail(delivery_id, "ack.delivery_id", "known delivery", delivery_id, "ack references an unknown consumer delivery")
            continue
        if ack.get("consumer_offset") != delivery.get("consumer_offset"):
            verification.fail(delivery_id, "ack.offset", delivery.get("consumer_offset"), ack.get("consumer_offset"), "ack offset does not correspond to the delivery")
        if ack.get("global_offset") != delivery.get("global_offset"):
            verification.fail(delivery_id, "ack.global_offset", delivery.get("global_offset"), ack.get("global_offset"), "ack global offset does not correspond to the delivery")
    for key, count in ack_keys.items():
        if count > 1:
            verification.fail(key[1], "ack.duplicate", 1, count, f"consumer {key[0]} acknowledged the same delivery more than once")
        acked_at = ""
        for _, ack in acks:
            if str(ack.get("consumer_id", "")) == key[0] and str(ack.get("delivery_id", "")) == key[1]:
                acked_at = str(ack.get("acked_at", ""))
        for _, delivered_at in deliveries_by_key.get(key, []):
            if acked_at and delivered_at > acked_at:
                verification.fail(
                    key[1],
                    "ack.post_ack_redelivery",
                    f"no delivery after {acked_at}",
                    delivered_at,
                    f"consumer {key[0]} received a delivery after acknowledging this message",
                )

    recovery = None
    recovery_path = data_dir / "recovery-report.json"
    if recovery_path.exists():
        try:
            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            verification.fail("", "recovery-report.json", "valid JSON", str(exc), "invalid recovery report")

    offsets = None
    offsets_path = data_dir / "consumer-offsets.json"
    if offsets_path.exists():
        try:
            offsets = json.loads(offsets_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            verification.fail("", "consumer-offsets.json", "valid JSON", str(exc), "invalid consumer offsets snapshot")
        if offsets and isinstance(offsets.get("consumers"), dict):
            for consumer_id, state in offsets["consumers"].items():
                expected_next = len(unique_offsets_by_consumer.get(consumer_id, [])) + 1
                if state.get("next_offset") != expected_next:
                    verification.fail("", "consumer-offsets.next_offset", expected_next, state.get("next_offset"), f"consumer {consumer_id} next offset is inconsistent with delivery log")

    verification.errors.sort(key=lambda error: (error["delivery_id"], error["field"], error["message"]))
    summary = {
        "messages": len(journals["messages"]),
        "deliveries": len(deliveries),
        "acks": len(acks),
        "consumers": len(unique_offsets_by_consumer),
        "exactly_once_messages": len(exact_sequences),
        "downgrades": conversion_counts["downgrade"],
        "upcasts": conversion_counts["upcast"],
        "same_version": conversion_counts["same"],
        "stripped_field_instances": stripped_instances,
        "filled_field_instances": filled_instances,
        "semantics": dict(sorted(semantic_counts.items())),
        "recovery": {
            "rebuilt_dedup_entries": (recovery or {}).get("rebuilt_dedup_entries", 0),
            "tail_action": (recovery or {}).get("tail_action", "not_recorded"),
            "truncated_bytes": (recovery or {}).get("truncated_bytes", 0),
            "recovered_offsets": (recovery or {}).get("recovered_offsets", {}),
        },
    }
    return {
        "ok": not verification.errors,
        "code": "OK" if not verification.errors else "E_VERIFY",
        "summary": summary,
        "errors": verification.errors,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify conversion and delivery-semantic consistency")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = verify_session(args.data_dir)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
