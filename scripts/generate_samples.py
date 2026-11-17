#!/usr/bin/env python3
"""Generate deterministic local samples and tune an equal-threshold boundary."""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_drift_workbench.canonical import quantize
from intent_drift_workbench.engine import WorkbenchEngine
from intent_drift_workbench.updates import load_plan

SEED = 20261117
CATEGORIES = ["account", "billing", "refund", "shipping", "technical"]
MASTER_TERMS = [
    "invoice", "refund", "charge", "payment", "receipt", "transaction",
    "delivery", "parcel", "tracking", "shipment", "courier",
    "login", "password", "profile", "account", "email",
    "error", "crash", "install", "update", "device",
    "账单", "退款", "扣费", "支付", "发票", "物流", "快递", "包裹", "配送",
    "账户", "密码", "登录", "资料", "报错", "崩溃", "安装", "更新", "设备",
]

BASE_TIME = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def iso_time(minutes: int) -> str:
    return (BASE_TIME + timedelta(minutes=minutes)).isoformat()


def turn(
    session_id: str,
    turn_no: int,
    speaker: str,
    text: str,
    minute: int,
    *,
    injection_id: str | None = None,
) -> dict[str, Any]:
    event = {
        "session_id": session_id,
        "turn_no": turn_no,
        "speaker": speaker,
        "text": text,
        "timestamp": iso_time(minute),
    }
    if injection_id is not None:
        event["injection_id"] = injection_id
    return event


def make_text(rng: random.Random, pool: list[str], extra: str | None = None) -> str:
    count = rng.randint(3, 5)
    selected = rng.sample(pool, count)
    if extra is not None:
        selected.insert(rng.randrange(len(selected) + 1), extra)
    return " ".join(selected)


def generate_events() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    pool_a = MASTER_TERMS[0:9]
    pool_b = MASTER_TERMS[9:19]
    pool_c = MASTER_TERMS[19:29]
    pool_d = MASTER_TERMS[29:39]
    sessions: list[dict[str, Any]] = []

    for turn_no in range(1, 13):
        pool = pool_a if turn_no <= 6 else pool_b
        extra = "payment" if turn_no == 3 else None
        sessions.append(turn("S01-billing-drift", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool, extra), turn_no))
    for turn_no in range(1, 11):
        extra = "courier" if turn_no == 1 else ("tracking" if turn_no == 4 else None)
        sessions.append(turn("S02-shipping-gap", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_c if turn_no < 6 else pool_a, extra), turn_no + 20))
    for turn_no in range(1, 9):
        extra = "tracking" if turn_no in {2, 6} else None
        sessions.append(turn("S03-account", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_b, extra), turn_no + 40))
    for turn_no in range(1, 11):
        extras = {3: "escalation", 5: "升级包"}
        sessions.append(turn("S04-technical", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_d, extras.get(turn_no)), turn_no + 60))
    for turn_no in range(1, 4):
        sessions.append(turn("S05-short-window", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_c), turn_no + 80))

    sessions.append({"session_id": "S-INJ-MISSING", "turn_no": 1, "speaker": "user",
                     "timestamp": iso_time(100), "injection_id": "INJ-MISSING-FIELD"})
    sessions.append(turn("S-INJ-MISSING", 2, "agent", make_text(rng, pool_a), 101))

    sessions.append(turn("S-INJ-GAP", 1, "user", make_text(rng, pool_b), 110))
    sessions.append(turn("S-INJ-GAP", 3, "agent", make_text(rng, pool_c), 111,
                         injection_id="INJ-TURN-GAP"))

    bad_timestamp = turn("S-INJ-TIMESTAMP", 1, "user", make_text(rng, pool_a), 120,
                         injection_id="INJ-BAD-TIMESTAMP")
    bad_timestamp["timestamp"] = "not-a-timestamp"
    sessions.append(bad_timestamp)
    sessions.append(turn("S-INJ-TIMESTAMP", 2, "agent", make_text(rng, pool_b), 121))

    for turn_no in range(1, 7):
        text = "unmappedtoken otherunknown" if turn_no == 3 else make_text(rng, pool_d)
        sessions.append(turn("S-INJ-EMPTY", turn_no, "user" if turn_no % 2 else "agent", text,
                             turn_no + 130,
                             injection_id="INJ-EMPTY-TERMS" if turn_no == 3 else None))

    for turn_no in range(1, 7):
        sessions.append(turn("S-INJ-TURN-EXC", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_a), turn_no + 140))
    for turn_no in range(1, 7):
        sessions.append(turn("S-INJ-WINDOW-EXC", turn_no, "user" if turn_no % 2 else "agent",
                             make_text(rng, pool_c), turn_no + 150))
    return sessions


def base_config(threshold: float = 0.35) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "intent_vocab_version": "intent-v1",
        "categories": CATEGORIES,
        "vocabulary": MASTER_TERMS,
        "fold_rules": {
            "version": "fold-v1",
            "lowercase": True,
            "nfkc": True,
            "strip_punctuation": True,
            "collapse_whitespace": True,
        },
        "window_size": 4,
        "weights": {
            "distribution_l1": 0.35,
            "js_divergence": 0.45,
            "novelty": 0.20,
        },
        "drift_threshold": threshold,
        "precision": 6,
        "concurrency": 4,
        "snapshot_interval": 1,
    }


def plan_document() -> dict[str, Any]:
    batches = [
        {
            "batch_id": "B001-turn-only",
            "turn_ops": [
                {
                    "op": "replace",
                    "session_id": "S01-billing-drift",
                    "turn_no": 7,
                    "event": turn(
                        "S01-billing-drift", 7, "agent",
                        "device crash error install report 设备 报错 崩溃",
                        207,
                    ),
                },
                {"op": "delete", "session_id": "S02-shipping-gap", "turn_no": 5},
                {
                    "op": "add",
                    "event": turn(
                        "S03-account", 9, "user",
                        "login password account email 登录 密码 账户",
                        249,
                    ),
                },
            ],
        },
        {
            "batch_id": "B002-vocab-only",
            "vocab_ops": [
                {"op": "add", "term": "escalation"},
                {"op": "add", "term": "升级包"},
                {"op": "remove", "term": "courier"},
            ],
        },
        {
            "batch_id": "B003-weight-only",
            "weight_patch": {
                "distribution_l1": 0.25,
                "js_divergence": 0.55,
                "novelty": 0.20,
            },
        },
        {
            "batch_id": "B004-mixed",
            "turn_ops": [
                {
                    "op": "replace",
                    "session_id": "S04-technical",
                    "turn_no": 6,
                    "event": turn(
                        "S04-technical", 6, "agent",
                        "escalation 升级包 device error 设备 报错",
                        266,
                    ),
                },
                {
                    "op": "replace",
                    "session_id": "S01-billing-drift",
                    "turn_no": 2,
                    "event": turn(
                        "S01-billing-drift", 2, "agent",
                        "invoice charge receipt payment 账单 扣费 发票",
                        202,
                    ),
                },
            ],
            "vocab_ops": [
                {"op": "remove", "term": "tracking"},
            ],
            "weight_patch": {
                "distribution_l1": 0.25,
                "js_divergence": 0.60,
                "novelty": 0.15,
            },
        },
    ]
    return {"schema_version": 1, "batches": batches}


def injection_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "injections": [
            {"id": "INJ-MISSING-FIELD", "kind": "invalid_turn", "expected_code": "E_TURN_INVALID"},
            {"id": "INJ-TURN-GAP", "kind": "turn_gap", "expected_code": "E_TURN_INVALID"},
            {"id": "INJ-BAD-TIMESTAMP", "kind": "invalid_timestamp", "expected_code": "E_TURN_INVALID"},
            {
                "id": "INJ-EMPTY-TERMS",
                "kind": "empty_terms",
                "session_id": "S-INJ-EMPTY",
                "turn_no": 3,
            },
        ],
        "force_exceptions": [
            {
                "id": "INJ-TURN-EXCEPTION",
                "kind": "turn_feature",
                "session_id": "S-INJ-TURN-EXC",
                "turn_no": 2,
            },
            {
                "id": "INJ-WINDOW-EXCEPTION",
                "kind": "window_score",
                "session_id": "S-INJ-WINDOW-EXC",
                "start_turn": 1,
            },
        ],
        "probes": [
            {
                "id": "INJ-ILLEGAL-WEIGHT",
                "kind": "illegal_weight",
                "patch": {"js_divergence": -0.01},
            },
            {
                "id": "INJ-TERM-REFERENCE",
                "kind": "term_reference",
                "term": "missingterm",
            },
            {"id": "INJ-THRESHOLD-EQUAL", "kind": "threshold_boundary"},
        ],
    }


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_events(path: Path, events: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.write("\n")


def tune_threshold(sample_dir: Path, config: dict[str, Any]) -> float:
    with tempfile.TemporaryDirectory(prefix=".threshold-tune-", dir=sample_dir) as temp_name:
        engine = WorkbenchEngine(
            config_path=sample_dir / "drift_config.json",
            events_path=sample_dir / "sessions.jsonl",
            plan_path=sample_dir / "update_plan.json",
            injections_path=sample_dir / "injections.json",
            snapshot_root=None,
            execution="serial",
        )
        engine.prepare()
        for batch in load_plan(sample_dir / "update_plan.json")["batches"]:
            engine.apply_batch(batch)
        result = engine.result()
        scores = sorted(
            {
                quantize(float(window["drift_score"]), int(config["precision"]))
                for window in result["windows"]
                if window.get("status") == "full" and window.get("drift_score") is not None
            }
        )
        positive = [score for score in scores if score > 0.0]
        if positive:
            return positive[len(positive) // 2]
        if scores:
            return scores[0]
        return 0.0


def verify_boundary(sample_dir: Path) -> list[dict[str, Any]]:
    engine = WorkbenchEngine(
        config_path=sample_dir / "drift_config.json",
        events_path=sample_dir / "sessions.jsonl",
        plan_path=sample_dir / "update_plan.json",
        injections_path=sample_dir / "injections.json",
        snapshot_root=None,
        execution="serial",
    )
    engine.prepare()
    for batch in load_plan(sample_dir / "update_plan.json")["batches"]:
        engine.apply_batch(batch)
    return engine.result()["boundary_alerts"]


def generate(sample_dir: Path) -> dict[str, Any]:
    sample_dir.mkdir(parents=True, exist_ok=True)
    events = generate_events()
    write_events(sample_dir / "sessions.jsonl", events)
    config = base_config()
    write_json(sample_dir / "drift_config.json", config)
    write_json(sample_dir / "update_plan.json", plan_document())
    write_json(sample_dir / "injections.json", injection_document())

    threshold = tune_threshold(sample_dir, config)
    config["drift_threshold"] = threshold
    write_json(sample_dir / "drift_config.json", config)
    boundary = verify_boundary(sample_dir)
    if not boundary:
        raise RuntimeError("threshold tuning did not produce an equal-threshold boundary")
    manifest = {
        "seed": SEED,
        "event_objects": len(events),
        "loader_valid_events_expected": len(events) - 3,
        "feature_turns_expected": len(events) - 4,
        "threshold": threshold,
        "boundary_alerts": boundary,
        "files": [
            "sessions.jsonl",
            "drift_config.json",
            "update_plan.json",
            "injections.json",
        ],
    }
    write_json(sample_dir / "sample_manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "samples"), help="sample output directory")
    args = parser.parse_args()
    manifest = generate(Path(args.out).resolve())
    print(json.dumps({"status": "generated", "out": str(Path(args.out).resolve()), **manifest},
                     ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
