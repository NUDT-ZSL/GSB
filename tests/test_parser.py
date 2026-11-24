from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from smuggle_detector.io_utils import atomic_write_json
from smuggle_detector.parsing import (
    compute_input_digest,
    parse_messages,
    parse_rules_v1,
    parse_rules_v2,
    parse_sample_dir,
)


def test_message_parser_rejects_bad_records_and_keeps_valid(tmp_path: Path) -> None:
    valid_payload = b"GET / HTTP/1.1\r\n\r\n".hex()
    records = [
        {
            "connection_id": "conn-ok",
            "direction": "c2s",
            "seq": 0,
            "ts_ms": 0,
            "payload_hex": valid_payload,
        },
        {
            "connection_id": "bad conn",
            "direction": "c2s",
            "seq": 0,
            "ts_ms": 0,
            "payload_hex": "",
        },
        {
            "connection_id": "conn-ok",
            "direction": "sideways",
            "seq": 0,
            "ts_ms": 0,
            "payload_hex": "",
        },
        {
            "connection_id": "conn-ok",
            "direction": "c2s",
            "seq": -1,
            "ts_ms": 0,
            "payload_hex": "",
        },
        {
            "connection_id": "conn-ok",
            "direction": "c2s",
            "seq": 1,
            "ts_ms": -2,
            "payload_hex": "",
        },
        {
            "connection_id": "conn-ok",
            "direction": "c2s",
            "seq": 2,
            "ts_ms": 0,
            "payload_hex": "abc",
        },
        {
            "connection_id": "conn-ok",
            "direction": "c2s",
            "seq": 3,
            "ts_ms": 0,
            "payload_hex": "zz",
        },
    ]
    lines = [
        json.dumps(item) if isinstance(item, dict) else item for item in records
    ]
    lines.insert(3, "{not-json")
    (tmp_path / "messages.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    parsed, rejections = parse_messages(tmp_path)
    assert len(parsed) == 1
    assert parsed[0].connection_id == "conn-ok"
    assert len(rejections) == 7
    assert {item.source_category for item in rejections} == {"messages"}
    assert {item.code for item in rejections} >= {
        "E_BAD_CONN",
        "E_BAD_DIRECTION",
        "E_BAD_SEQ",
        "E_BAD_TS",
        "E_BAD_HEX",
        "E_BAD_RECORD",
    }


def test_v1_parser_rejects_individual_bad_rules(tmp_path: Path) -> None:
    atomic_write_json(
        tmp_path / "rules_v1.json",
        {
            "relative_epoch_ms": 10,
            "rules": [
                {
                    "name": "GOOD_RULE",
                    "relative_effective_seconds": 2,
                    "patterns": ["cl+te"],
                    "enabled": True,
                },
                {
                    "name": "bad name",
                    "relative_effective_seconds": 1,
                    "patterns": ["cl+te"],
                    "enabled": True,
                },
                {
                    "name": "BAD_EFFECTIVE",
                    "relative_effective_seconds": -1,
                    "patterns": ["cl+te"],
                    "enabled": True,
                },
                {
                    "name": "BAD_ENABLED",
                    "relative_effective_seconds": 1,
                    "patterns": ["cl+te"],
                    "enabled": 1,
                },
                {
                    "name": "BAD_PATTERN",
                    "relative_effective_seconds": 1,
                    "patterns": ["not-a-detector"],
                    "enabled": True,
                },
                {
                    "name": "DISABLED_TOMBSTONE",
                    "relative_effective_seconds": 3,
                    "patterns": [],
                    "enabled": False,
                },
            ],
        },
    )
    records, rejections = parse_rules_v1(tmp_path)
    assert len(records) == 2
    good = next(item for item in records if item.canonical_name == "GOOD_RULE")
    assert good.effective_ms == 2010
    assert good.direction == "both"
    assert good.patterns == ("CL_TE",)
    assert len(rejections) == 4
    assert {item.source_category for item in rejections} == {"rules_v1"}


def test_v2_parser_joins_metadata_and_shards_and_rejects_bad_rows(
    tmp_path: Path,
) -> None:
    shard_dir = tmp_path / "rules_v2_shards"
    shard_dir.mkdir()
    atomic_write_json(
        shard_dir / "rules.json",
        {
            "rules": [
                {"canonical_name": "GOOD_RULE", "patterns": ["CL_TE"]},
                {"canonical_name": "ORPHAN_RULE", "patterns": ["DUP_HEADER_CONFLICT"]},
            ]
        },
    )
    connection = sqlite3.connect(tmp_path / "rules_v2.sqlite3")
    try:
        connection.execute(
            "CREATE TABLE rule_meta ("
            "canonical_name TEXT, effective_ms INTEGER, generation INTEGER, "
            "direction TEXT, tombstone INTEGER, shard_file TEXT, record_index INTEGER)"
        )
        connection.executemany(
            "INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("GOOD_RULE", 1000, 2, "c2s", 0, "rules.json", 0),
                ("GOOD_RULE", 1001, 2, "c2s", 0, "rules.json", 1),
                ("BAD_DIRECTION", 1000, 2, "up", 0, "rules.json", 2),
                ("BAD_GEN", 1000, 0, "c2s", 0, "rules.json", 3),
                ("MISSING_SHARD", 1000, 2, "c2s", 0, "missing.json", 4),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    records, rejections = parse_rules_v2(tmp_path)
    assert records
    assert {item.canonical_name for item in records} == {"GOOD_RULE"}
    assert any(item.code == "E_BAD_DIRECTION_RULE" for item in rejections)
    assert any(item.code == "E_BAD_GENERATION" for item in rejections)
    assert any(item.code == "E_SHARD_MISSING" for item in rejections)
    assert any(
        item.code == "E_SHARD_MISMATCH"
        and "ORPHAN_RULE" in item.detail
        for item in rejections
    )


def test_sample_import_digest_is_independent_of_record_interleaving(
    sample_dir: Path, tmp_path: Path
) -> None:
    first = parse_sample_dir(sample_dir)
    second_dir = tmp_path / "reordered"
    shutil.copytree(sample_dir, second_dir)
    lines = (second_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    lines.reverse()
    (second_dir / "messages.jsonl").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    second = parse_sample_dir(second_dir)
    assert compute_input_digest(first) == compute_input_digest(second)

