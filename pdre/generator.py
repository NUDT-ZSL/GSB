"""Deterministic local sample generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_SEED = 382
OBSERVED_CLOCK_MS = 1_767_225_600_000
ENDPOINT_ID = "ep-382-01"


def _guid(seed: int, name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"pdre-feedback/{seed}/{name}"))


def _digest(seed: int, name: str) -> str:
    raw = hashlib.sha256(f"pdre-feedback/{seed}/digest/{name}".encode("utf-8")).hexdigest()
    return f"sha256:{raw}"


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def generate_sample(output_dir: str | Path, seed: int = DEFAULT_SEED) -> dict[str, Path]:
    output = Path(output_dir)
    truth_dir = output / "truth"
    output.mkdir(parents=True, exist_ok=True)
    truth_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    risk_series = [int(value) for value in rng.integers(15, 90, size=32)]

    names = [
        "root-alpha",
        "alpha-child",
        "alpha-grand",
        "root-beta",
        "beta-child",
        "fork-child",
        "fork-parent-a",
        "fork-root-a",
        "fork-parent-b",
        "fork-root-b",
        "fork-desc",
        "cycle-a",
        "cycle-b",
        "cycle-c",
        "self",
        "orphan",
        "ghost",
        "undeclared",
    ]
    guid = {name: _guid(seed, name) for name in names}

    digest_names = [
        "alpha-action",
        "beta-old",
        "beta-new",
        "gamma-window",
        "multi",
        "tombstone",
        "stable",
        "fork-policy",
    ]
    digest = {name: _digest(seed, name) for name in digest_names}

    lineage: list[dict[str, Any]] = [
        {"process_guid": guid["root-alpha"], "parent_process_guid": None, "session_id": "sess-root", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["alpha-child"], "parent_process_guid": guid["root-alpha"], "session_id": "sess-alpha-1", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["alpha-grand"], "parent_process_guid": guid["alpha-child"], "session_id": "sess-alpha-2", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["root-beta"], "parent_process_guid": None, "session_id": "sess-root", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["beta-child"], "parent_process_guid": guid["root-beta"], "session_id": "sess-beta-1", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-root-a"], "parent_process_guid": None, "session_id": "sess-fork-root-a", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-parent-a"], "parent_process_guid": guid["fork-root-a"], "session_id": "sess-fork-a", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-root-b"], "parent_process_guid": None, "session_id": "sess-fork-root-b", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-parent-b"], "parent_process_guid": guid["fork-root-b"], "session_id": "sess-fork-b", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-child"], "parent_process_guid": guid["fork-parent-a"], "session_id": "sess-fork-a", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-child"], "parent_process_guid": guid["fork-parent-b"], "session_id": "sess-fork-b", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["fork-desc"], "parent_process_guid": guid["fork-child"], "session_id": "sess-fork-desc", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["cycle-a"], "parent_process_guid": guid["cycle-b"], "session_id": "sess-cycle", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["cycle-b"], "parent_process_guid": guid["cycle-c"], "session_id": "sess-cycle", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["cycle-c"], "parent_process_guid": guid["cycle-a"], "session_id": "sess-cycle", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["self"], "parent_process_guid": guid["self"], "session_id": "sess-self", "endpoint_id": ENDPOINT_ID},
        {"process_guid": guid["orphan"], "parent_process_guid": guid["ghost"], "session_id": "sess-missing", "endpoint_id": ENDPOINT_ID},
    ]

    def event(
        seq: int,
        process_name: str,
        parent_name: str | None,
        policy_key: str,
        action_kind: str,
        offset_sec: int,
        digest_name: str,
    ) -> dict[str, Any]:
        return {
            "event_seq": seq,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid[process_name],
            "parent_process_guid": None if parent_name is None else guid[parent_name],
            "policy_key": policy_key,
            "action_kind": action_kind,
            "risk_level": risk_series[(seq - 1) % len(risk_series)],
            "observed_ms": OBSERVED_CLOCK_MS + offset_sec * 1000,
            "payload_digest": digest[digest_name],
        }

    events: list[dict[str, Any]] = [
        event(1, "root-alpha", None, "pdre:multi-z", "PROCESS_CREATE", 1, "multi"),
        event(2, "alpha-child", "root-alpha", "pdre:alpha-action", "FILE_WRITE", 2, "alpha-action"),
        event(3, "alpha-grand", "alpha-child", "pdre:stable", "NETWORK_CONNECT", 3, "stable"),
        event(4, "alpha-grand", "alpha-child", "pdre:tombstoned", "DNS_QUERY", 4, "tombstone"),
        event(5, "root-beta", None, "pdre:gamma-window", "REGISTRY_WRITE", 0, "gamma-window"),
        event(6, "beta-child", "root-beta", "pdre:beta-match", "FILE_WRITE", 2, "beta-old"),
        event(7, "beta-child", "root-beta", "pdre:beta-match", "FILE_WRITE", 3, "beta-new"),
        event(8, "fork-child", "fork-parent-a", "pdre:fork-policy", "PROCESS_CREATE", 1, "fork-policy"),
        event(9, "fork-desc", "fork-child", "pdre:fork-policy", "IMAGE_LOAD", 2, "fork-policy"),
        event(10, "cycle-a", "cycle-b", "pdre:none", "PROCESS_EXIT", 1, "stable"),
        event(11, "cycle-b", "cycle-c", "pdre:none", "PROCESS_EXIT", 2, "stable"),
        event(12, "self", "self", "pdre:none", "PROCESS_TERMINATE", 1, "stable"),
        event(13, "orphan", "ghost", "pdre:none", "FILE_DELETE", 1, "stable"),
        {
            "event_seq": 14,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid["undeclared"],
            "parent_process_guid": None,
            "policy_key": "pdre:none",
            "action_kind": "DNS_QUERY",
            "risk_level": 33,
            "observed_ms": OBSERVED_CLOCK_MS + 5000,
            "payload_digest": digest["stable"],
        },
    ]

    invalid_events: list[dict[str, Any]] = [
        {
            "event_seq": 901,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid["root-alpha"],
            "parent_process_guid": None,
            "policy_key": "pdre:invalid-action-kind",
            "action_kind": "NOT_A_REAL_ACTION",
            "risk_level": 20,
            "observed_ms": OBSERVED_CLOCK_MS,
            "payload_digest": digest["stable"],
        },
        {
            "event_seq": 902,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid["root-alpha"],
            "parent_process_guid": None,
            "policy_key": "pdre:invalid-risk",
            "action_kind": "FILE_WRITE",
            "risk_level": 101,
            "observed_ms": OBSERVED_CLOCK_MS,
            "payload_digest": digest["stable"],
        },
        {
            "event_seq": 903,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid["root-alpha"],
            "parent_process_guid": None,
            "policy_key": "pdre:invalid-time",
            "action_kind": "FILE_WRITE",
            "risk_level": 20,
            "observed_ms": -1,
            "payload_digest": digest["stable"],
        },
        {
            "event_seq": 904,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": guid["root-alpha"],
            "parent_process_guid": None,
            "policy_key": "pdre:invalid-digest",
            "action_kind": "FILE_WRITE",
            "risk_level": 20,
            "observed_ms": OBSERVED_CLOCK_MS,
            "payload_digest": "sha256:not-a-digest",
        },
        {
            "event_seq": 905,
            "endpoint_id": ENDPOINT_ID,
            "process_guid": "bad guid with spaces!",
            "parent_process_guid": None,
            "policy_key": "pdre:invalid-guid",
            "action_kind": "FILE_WRITE",
            "risk_level": 20,
            "observed_ms": OBSERVED_CLOCK_MS,
            "payload_digest": digest["stable"],
        },
    ]

    policies_v1: list[dict[str, Any]] = [
        {"policy_key": "PDRE:ALPHA-ACTION", "effective_start_sec": -600, "effective_end_sec": 3600, "match_string": digest["alpha-action"], "action_code": "AUDIT"},
        {"policy_key": "pdre:beta-match", "effective_start_sec": -600, "effective_end_sec": 3600, "match_string": digest["beta-old"], "action_code": "AUDIT"},
        {"policy_key": "pdre:gamma-window", "effective_start_sec": -600, "effective_end_sec": -100, "match_string": digest["gamma-window"], "action_code": "ALERT"},
        {"policy_key": "pdre:multi-a", "effective_start_sec": -600, "effective_end_sec": 3600, "match_string": digest["multi"], "action_code": "AUDIT"},
        {"policy_key": "pdre:tombstoned", "effective_start_sec": -600, "effective_end_sec": 3600, "match_string": digest["tombstone"], "action_code": "AUDIT"},
        {"policy_key": "pdre:stable", "effective_start_sec": -60, "effective_end_sec": 600, "match_string": digest["stable"], "action_code": "AUDIT"},
    ]

    policies_v2: list[dict[str, Any]] = [
        {"canonical_policy_key": "pdre:alpha-action", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 500_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["alpha-action"], "action_code": "BLOCK", "tombstone": False},
        {"canonical_policy_key": "pdre:beta-match", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 500_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["beta-new"], "action_code": "AUDIT", "tombstone": False},
        {"canonical_policy_key": "pdre:gamma-window", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 200_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["gamma-window"], "action_code": "ALERT", "tombstone": False},
        {"canonical_policy_key": "pdre:multi-b", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 500_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["multi"], "action_code": "AUDIT", "tombstone": False},
        {"canonical_policy_key": "pdre:multi-z", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 500_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["multi"], "action_code": "BLOCK", "tombstone": False},
        {"canonical_policy_key": "pdre:fork-policy", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 500_000, "effective_end_ms": OBSERVED_CLOCK_MS + 3_600_000, "match_string": digest["fork-policy"], "action_code": "ISOLATE", "tombstone": False},
        {"canonical_policy_key": "pdre:tombstoned", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 1_000, "effective_end_ms": None, "tombstone": True},
        {"canonical_policy_key": "pdre:stable", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS - 60_000, "effective_end_ms": OBSERVED_CLOCK_MS + 600_000, "match_string": digest["stable"], "action_code": "AUDIT", "tombstone": False},
    ]

    invalid_lineage: list[dict[str, Any]] = [
        {"process_guid": "bad lineage guid!", "parent_process_guid": None, "session_id": "sess-bad"},
        {"process_guid": guid["root-alpha"], "parent_process_guid": guid["root-beta"], "session_id": ""},
    ]
    invalid_policies_v1: list[dict[str, Any]] = [
        {"policy_key": "pdre:bad-v1-seconds", "effective_start_sec": "soon", "match_string": digest["stable"], "action_code": "AUDIT"},
    ]
    invalid_policies_v2: list[dict[str, Any]] = [
        {"canonical_policy_key": "pdre:bad-v2-generation", "rule_generation": 0, "effective_start_ms": OBSERVED_CLOCK_MS, "match_string": digest["stable"], "action_code": "AUDIT", "tombstone": False},
        {"canonical_policy_key": "pdre:bad-tombstone", "rule_generation": 2, "effective_start_ms": OBSERVED_CLOCK_MS, "tombstone": "yes"},
    ]

    event_records = events + invalid_events
    lineage_records = lineage + invalid_lineage
    v1_records = policies_v1 + invalid_policies_v1
    v2_records = policies_v2 + invalid_policies_v2

    paths = {
        "events": output / "events.jsonl",
        "lineage": output / "lineage.jsonl",
        "policies_v1": output / "policies_v1.jsonl",
        "policies_v2": output / "policies_v2.jsonl",
        "config": output / "sample_config.json",
        "truth": truth_dir / "sender_truth.jsonl",
        "manifest": truth_dir / "manifest.json",
    }
    _write_jsonl(paths["events"], event_records)
    with paths["events"].open("a", encoding="utf-8") as handle:
        handle.write('{"event_seq": 906, "broken_json": \n')
    _write_jsonl(paths["lineage"], lineage_records)
    _write_jsonl(paths["policies_v1"], v1_records)
    _write_jsonl(paths["policies_v2"], v2_records)

    config_payload = {
        "seed": seed,
        "observed_clock_ms": OBSERVED_CLOCK_MS,
        "digest_algorithm": "sha256",
        "endpoint_id": ENDPOINT_ID,
        "schema": "pdre-local-sample-v1",
    }
    paths["config"].write_text(json.dumps(config_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    truth_entries: list[dict[str, Any]] = []
    batch_number = 0

    def add_truth(category: str, filename: str, raw_records: list[dict[str, Any]]) -> None:
        nonlocal batch_number
        for index, record in enumerate(raw_records, start=1):
            batch_number += 1
            truth_entries.append(
                {
                    "sync_marker": f"sync-{seed}-{category}-{index:04d}",
                    "global_sequence": batch_number,
                    "source_category": category,
                    "source_file": filename,
                    "record_seq": index,
                    "raw_record": record,
                    "raw_record_sha256": hashlib.sha256(
                        json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest(),
                }
            )

    add_truth("event", "events.jsonl", event_records)
    truth_entries.append(
        {
            "sync_marker": f"sync-{seed}-event-0906-malformed",
            "global_sequence": batch_number + 1,
            "source_category": "event",
            "source_file": "events.jsonl",
            "record_seq": len(event_records) + 1,
            "raw_line": '{"event_seq": 906, "broken_json": ',
        }
    )
    add_truth("lineage", "lineage.jsonl", lineage_records)
    add_truth("policy_v1", "policies_v1.jsonl", v1_records)
    add_truth("policy_v2", "policies_v2.jsonl", v2_records)
    _write_jsonl(paths["truth"], truth_entries)

    manifest = {
        "seed": seed,
        "observed_clock_ms": OBSERVED_CLOCK_MS,
        "files": {
            name: {
                "path": str(path.relative_to(output)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
            for name, path in paths.items()
            if name not in {"truth", "manifest"} and path.exists()
        },
        "truth_entries": len(truth_entries),
        "sync_marker_prefix": f"sync-{seed}-",
    }
    paths["manifest"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths
