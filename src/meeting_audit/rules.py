from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from .canonical import atomic_write_bytes, canonical_bytes, content_key
from .codes import BUSINESS_CODES, E_RULE_MIGRATION_MISMATCH
from .config import AuditConfig, DEFAULT_CONFIG


RULE_ALGEBRA_V1 = "v1-flat-seconds"
RULE_ALGEBRA_V2 = "v2-sharded-ms-sqlite"

DEFAULT_ANOMALY_BY_METRIC = {
    "turn_interval_ms": "rapid_switch",
    "preceding_silence_ms": "silence_too_long",
    "energy_ratio_q": "speaker_preemption",
    "speech_claim": "normal_switch",
}

DOWNGRADE_DISCARD_PATHS = (
    "condition.nested_policy",
    "condition.all_of",
    "advanced",
    "lineage.completion_notes",
)


def default_thresholds(config: AuditConfig = DEFAULT_CONFIG) -> dict[str, int]:
    return {
        "rapid_switch_ms": config.rapid_switch_ms,
        "silence_long_ms": config.silence_long_ms,
        "preempt_energy_ratio_q": config.preempt_energy_ratio_q,
        "min_stable_turn_ms": config.min_stable_turn_ms,
    }


def build_default_v1_rules(config: AuditConfig = DEFAULT_CONFIG) -> dict[str, Any]:
    """Build the fixed delivered v1 flat JSON document."""
    thresholds = default_thresholds(config)
    rows = [
        ("V1-R0001", 0.400, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0002", 3.700, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0003", 6.800, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0004", 9.700, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0005", 11.380, "turn_interval_ms", "lt", config.rapid_switch_ms, "normal_switch"),
        # Deliberately omit anomaly_type on one row so upgrade completion is observable.
        ("V1-R0006", 12.120, "turn_interval_ms", "lt", config.rapid_switch_ms, None),
        ("V1-R0007", 15.000, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0008", 16.200, "energy_ratio_q", "gte", config.preempt_energy_ratio_q, "speaker_preemption"),
        ("V1-R0009", 21.050, "preceding_silence_ms", "gte", config.silence_long_ms, None),
        ("V1-R0010", 24.100, "speech_claim", "exists", 0, "normal_switch"),
        ("V1-R0011", 27.350, "speech_claim", "exists", 0, "normal_switch"),
    ]
    rules: list[dict[str, Any]] = []
    for rule_id, seconds, metric, comparator, threshold, anomaly_type in rows:
        row: dict[str, Any] = {
            "rule_id": rule_id,
            "time_second": seconds,
            "metric": metric,
            "comparator": comparator,
            "threshold_value": threshold,
            "threshold_source": "default_profile",
        }
        if anomaly_type is not None:
            row["anomaly_type"] = anomaly_type
        rules.append(row)

    return {
        "schema": "meeting-audit/turn-rules-v1",
        "generation": "v1",
        "rule_algebra": RULE_ALGEBRA_V1,
        "time_base": "relative_seconds",
        "rule_version": "v1.0.0",
        "thresholds": thresholds,
        "rules": rules,
    }


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_v1_rules(doc: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    atomic_write_bytes(path, canonical_bytes(doc) + b"\n")
    return path


def _seconds_to_ms(value: Any) -> int:
    # v1 sample rows use three-decimal decimal seconds; round is explicit and stable.
    return int(round(float(value) * 1000.0))


def migrate_v1_to_v2(v1_doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if v1_doc.get("generation") != "v1":
        raise ValueError("migrate_v1_to_v2 expects generation=v1")
    thresholds = dict(v1_doc.get("thresholds") or {})
    v2_rules: list[dict[str, Any]] = []
    completed_items: list[dict[str, Any]] = []

    for index, row in enumerate(v1_doc.get("rules", []), start=1):
        rule_id = str(row.get("rule_id") or f"V2-R{index:04d}")
        completed_fields: list[str] = []
        metric = str(row.get("metric", "normal_switch"))
        anomaly_type = row.get("anomaly_type")
        if not anomaly_type:
            anomaly_type = DEFAULT_ANOMALY_BY_METRIC.get(metric, "normal_switch")
            completed_fields.append("anomaly_type")

        threshold_source = row.get("threshold_source")
        if not threshold_source or threshold_source == "default_profile":
            threshold_source = f"v1:{v1_doc.get('rule_version', 'v1')}:thresholds.{metric}"
            completed_fields.append("threshold_source")

        at_ms = row.get("at_ms")
        if at_ms is None:
            at_ms = _seconds_to_ms(row["time_second"])
            completed_fields.append("at_ms_from_relative_seconds")

        condition = {
            "metric": metric,
            "comparator": row.get("comparator", "exists"),
            "threshold": row.get("threshold_value", 0),
            "unit": "ms" if str(metric).endswith("_ms") else "q",
            # Nested algebra is a v2-only enhancement. Downgrade drops it explicitly.
            "nested_policy": {"all_of": ["time_match", "condition_match"], "hold_frames": 3},
        }
        rule = {
            "rule_id": rule_id.replace("V1-", "V2-"),
            "source_rule_id": rule_id,
            "at_ms": int(at_ms),
            "anomaly_type": anomaly_type,
            "generation": "v2",
            "rule_algebra": RULE_ALGEBRA_V2,
            "rule_version": "v2.0.0",
            "threshold_source": threshold_source,
            "condition": condition,
            "lineage": {
                "migrated_from": "v1",
                "v1_time_second": row.get("time_second"),
                "completed_fields": completed_fields,
                "completion_notes": [
                    f"{field} was filled by deterministic v1->v2 default policy"
                    for field in completed_fields
                ],
            },
        }
        v2_rules.append(rule)
        if completed_fields:
            completed_items.append({"rule_id": rule["rule_id"], "completed_fields": completed_fields})

    v2_doc = {
        "schema": "meeting-audit/turn-rules-v2",
        "generation": "v2",
        "rule_algebra": RULE_ALGEBRA_V2,
        "time_base": "absolute_ms",
        "rule_version": "v2.0.0",
        "thresholds": thresholds,
        "rules": v2_rules,
        "migration_report": {
            "direction": "v1_to_v2",
            "source_rule_version": v1_doc.get("rule_version", "v1.0.0"),
            "rule_count": len(v2_rules),
            "completed_item_count": len(completed_items),
            "completed_items": completed_items,
        },
    }
    return v2_doc, v2_doc["migration_report"]


def write_v2_sharded(
    v2_doc: dict[str, Any], output_dir: str | Path, sqlite_path: str | Path | None = None
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    by_type: dict[str, list[dict[str, Any]]] = {}
    for rule in v2_doc.get("rules", []):
        by_type.setdefault(str(rule["anomaly_type"]), []).append(rule)

    for anomaly_type, rules in sorted(by_type.items()):
        shard = {
            "schema": "meeting-audit/turn-rules-v2-shard",
            "generation": "v2",
            "anomaly_type": anomaly_type,
            "rules": rules,
        }
        path = out / f"{anomaly_type}.json"
        atomic_write_bytes(path, canonical_bytes(shard) + b"\n")
        paths[anomaly_type] = path

    index_path = out / "index.json"
    index_doc = {k: str(v.name) for k, v in paths.items()}
    atomic_write_bytes(index_path, canonical_bytes(index_doc) + b"\n")
    paths["index"] = index_path

    db_path = Path(sqlite_path) if sqlite_path is not None else out / "rules_v2.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE rule_meta (
                rule_id TEXT PRIMARY KEY,
                source_rule_id TEXT NOT NULL,
                at_ms INTEGER NOT NULL,
                anomaly_type TEXT NOT NULL,
                generation TEXT NOT NULL,
                rule_algebra TEXT NOT NULL,
                rule_version TEXT NOT NULL,
                shard_file TEXT NOT NULL,
                threshold_source TEXT NOT NULL,
                content_key TEXT NOT NULL
            )
            """
        )
        for rule in v2_doc.get("rules", []):
            shard_file = f"{rule['anomaly_type']}.json"
            con.execute(
                "INSERT INTO rule_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rule["rule_id"],
                    rule["source_rule_id"],
                    int(rule["at_ms"]),
                    rule["anomaly_type"],
                    rule["generation"],
                    rule.get("rule_algebra", RULE_ALGEBRA_V2),
                    rule["rule_version"],
                    shard_file,
                    rule["threshold_source"],
                    content_key(rule),
                ),
            )
        report = v2_doc.get("migration_report") or {}
        con.execute(
            "CREATE TABLE migration_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for key, value in report.items():
            con.execute(
                "INSERT INTO migration_meta VALUES (?, ?)",
                (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
            )
        con.execute(
            "INSERT INTO migration_meta VALUES (?, ?)",
            ("thresholds", json.dumps(v2_doc.get("thresholds", {}), sort_keys=True)),
        )
        con.execute(
            "INSERT INTO migration_meta VALUES (?, ?)",
            ("rule_version", json.dumps(v2_doc.get("rule_version", "v2.0.0"))),
        )
        con.commit()
    finally:
        con.close()
    paths["sqlite"] = db_path
    return paths


def read_v2_sharded(shard_dir: str | Path, sqlite_path: str | Path | None = None) -> dict[str, Any]:
    directory = Path(shard_dir)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    rules: list[dict[str, Any]] = []
    for anomaly_type, filename in sorted(index.items()):
        doc = json.loads((directory / str(filename)).read_text(encoding="utf-8"))
        rules.extend(doc.get("rules", []))
    rules.sort(key=lambda row: (int(row["at_ms"]), str(row["rule_id"])))

    db_path = Path(sqlite_path) if sqlite_path is not None else directory / "rules_v2.sqlite"
    meta_rows: list[dict[str, Any]] = []
    thresholds: dict[str, Any] = {}
    rule_version = "v2.0.0"
    if db_path.exists():
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            for row in con.execute(
                "SELECT rule_id, at_ms, anomaly_type, generation, rule_version, content_key FROM rule_meta ORDER BY at_ms, rule_id"
            ):
                meta_rows.append(dict(row))
            try:
                meta = {
                    row["key"]: json.loads(row["value"])
                    for row in con.execute("SELECT key, value FROM migration_meta")
                }
                thresholds = dict(meta.get("thresholds") or {})
                rule_version = str(meta.get("rule_version") or rule_version)
            except sqlite3.OperationalError:
                pass
        finally:
            con.close()
    return {
        "schema": "meeting-audit/turn-rules-v2",
        "generation": "v2",
        "rule_algebra": RULE_ALGEBRA_V2,
        "time_base": "absolute_ms",
        "rule_version": rule_version,
        "thresholds": thresholds,
        "rules": rules,
        "sqlite_meta": meta_rows,
    }


def _discard_nested(value: dict[str, Any], path: str) -> tuple[Any, list[dict[str, str]]]:
    parts = path.split(".")
    current = value
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return value, []
        current = current[part]
    leaf = parts[-1]
    if isinstance(current, dict) and leaf in current:
        discarded_value = current.pop(leaf)
        return discarded_value, [
            {"path": path, "reason": "v1_flat_schema_cannot_express_nested_rule"}
        ]
    return None, []


def migrate_v2_to_v1(v2_doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if v2_doc.get("generation") != "v2":
        raise ValueError("migrate_v2_to_v1 expects generation=v2")
    rows: list[dict[str, Any]] = []
    discarded_items: list[dict[str, Any]] = []

    for rule in v2_doc.get("rules", []):
        flat = deepcopy(rule)
        discards: list[dict[str, str]] = []
        for path in DOWNGRADE_DISCARD_PATHS:
            _value, discarded = _discard_nested(flat, path)
            discards.extend(discarded)

        condition = flat.pop("condition", {}) if isinstance(flat.get("condition"), dict) else {}
        if "threshold" in condition:
            flat["threshold_value"] = condition["threshold"]
        if "metric" in condition:
            flat["metric"] = condition["metric"]
        if "comparator" in condition:
            flat["comparator"] = condition["comparator"]
        flat.pop("rule_algebra", None)
        flat.pop("lineage", None)
        flat.pop("generation", None)
        flat["time_second"] = int(flat.pop("at_ms")) / 1000.0
        flat["generation"] = "v1"
        if discards:
            discarded_items.append({"rule_id": flat.get("rule_id"), "discarded": discards})
        rows.append(flat)

    v1_doc = {
        "schema": "meeting-audit/turn-rules-v1-downgraded",
        "generation": "v1",
        "rule_algebra": RULE_ALGEBRA_V1,
        "time_base": "relative_seconds",
        "rule_version": "v1.0.0-downgraded",
        "thresholds": v2_doc.get("thresholds", {}),
        "rules": rows,
        "downgrade_report": {
            "direction": "v2_to_v1",
            "explicit_discard_policy": list(DOWNGRADE_DISCARD_PATHS),
            "discarded_item_count": len(discarded_items),
            "discarded_items": discarded_items,
        },
    }
    return v1_doc, v1_doc["downgrade_report"]


def thresholds_from_doc(doc: dict[str, Any], config: AuditConfig = DEFAULT_CONFIG) -> dict[str, int]:
    values = default_thresholds(config)
    values.update({k: int(v) for k, v in (doc.get("thresholds") or {}).items()})
    return values


def classify_turns(
    turn_points: list[dict[str, Any]],
    rule_doc: dict[str, Any] | None = None,
    config: AuditConfig = DEFAULT_CONFIG,
) -> list[dict[str, Any]]:
    thresholds = thresholds_from_doc(rule_doc or {}, config)
    classified: list[dict[str, Any]] = []
    for index, turn in enumerate(turn_points):
        previous = turn_points[index - 1] if index > 0 else None
        interval_ms = None if previous is None else int(turn["at_ms"]) - int(previous["at_ms"])
        silence_ms = turn.get("preceding_silence_ms")
        ratio_q = turn.get("energy_ratio_q")

        anomaly_type = "normal_switch"
        evidence: dict[str, int | None] = {
            "turn_interval_ms": interval_ms,
            "rapid_switch_ms": thresholds["rapid_switch_ms"],
            "preceding_silence_ms": silence_ms,
            "silence_long_ms": thresholds["silence_long_ms"],
            "energy_ratio_q": ratio_q,
            "preempt_energy_ratio_q": thresholds["preempt_energy_ratio_q"],
        }

        if turn.get("kind") == "acoustic_shift" and ratio_q is not None and ratio_q >= thresholds["preempt_energy_ratio_q"]:
            anomaly_type = "speaker_preemption"
        elif silence_ms is not None and silence_ms >= thresholds["silence_long_ms"]:
            anomaly_type = "silence_too_long"
        elif interval_ms is not None and 0 < interval_ms < thresholds["rapid_switch_ms"]:
            anomaly_type = "rapid_switch"

        row = dict(turn)
        row.update(
            {
                "anomaly_type": anomaly_type,
                "business_code": BUSINESS_CODES[anomaly_type],
                "turn_interval_ms": interval_ms,
                "classification_evidence": evidence,
                "rule_version": (rule_doc or {}).get("rule_version", config and "config-default"),
            }
        )
        classified.append(row)
    return classified


def classification_signature(classified: list[dict[str, Any]]) -> list[tuple[int, str, str]]:
    return [(int(row["at_ms"]), str(row["event_id"]), str(row["business_code"])) for row in classified]


def compare_rule_views(
    v1_doc: dict[str, Any],
    v2_doc: dict[str, Any],
    turn_points: list[dict[str, Any]],
    config: AuditConfig = DEFAULT_CONFIG,
) -> dict[str, Any]:
    first = classify_turns(turn_points, v1_doc, config)
    second = classify_turns(turn_points, v2_doc, config)
    left = classification_signature(first)
    right = classification_signature(second)
    mismatches = [
        {"v1": a, "v2": b}
        for a, b in zip(left, right)
        if a != b or len(left) != len(right)
    ]
    if len(left) != len(right):
        mismatches.append({"length": [len(left), len(right)]})
    return {
        "ok": not mismatches,
        "equivalent": not mismatches,
        "code": None if not mismatches else E_RULE_MIGRATION_MISMATCH,
        "v1_signature": left,
        "v2_signature": right,
        "mismatches": mismatches,
        "v1_key": content_key(v1_doc),
        "v2_key": content_key(v2_doc),
    }
