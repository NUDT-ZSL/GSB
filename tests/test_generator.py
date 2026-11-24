from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from smuggling_engine.generator import generate_sample


def test_generator_is_deterministic(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    manifest_one = generate_sample(first, seed=777, scenario="standard")
    manifest_two = generate_sample(second, seed=777, scenario="standard")
    assert manifest_one == manifest_two
    for relative in (
        "messages.jsonl",
        "rules_v1.json",
        "rules_v2/part-000.json",
        "rules_v2/part-001.json",
        "sync_truth.json",
        "manifest.json",
    ):
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
    def rules(path):
        connection = sqlite3.connect(str(path))
        try:
            return connection.execute(
                "SELECT * FROM v2_rules ORDER BY canonical_name"
            ).fetchall()
        finally:
            connection.close()

    assert rules(first / "rules_v2.sqlite3") == rules(second / "rules_v2.sqlite3")


def test_truth_contains_sender_bytes_and_sync_markers(tmp_path):
    directory = tmp_path / "truth"
    generate_sample(directory, seed=55)
    truth = json.loads((directory / "sync_truth.json").read_text(encoding="utf-8"))
    assert truth["messages"]
    for message in truth["messages"]:
        payload = bytes.fromhex(message["sender_raw_hex"])
        assert message["payload_hex"] == message["sender_raw_hex"]
        assert message["payload_sha256"] == hashlib.sha256(payload).hexdigest()
        assert message["sync_marker"].startswith("SYNC-")
        assert message["sender"] in ("client", "server")
    markers = {item["canonical_name"] for item in truth["rule_authority"]}
    assert "cl-te" in markers
    assert "cl-te.strict" in markers

