from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, canonical_line, sha256_bytes
from .config import Rules
from .formats import write_v1, write_v2, write_v3
from .graph import DiagnosticEngine
from .turns import ParsedTurn


DEFAULT_SEED = 20261127


def _rules_payload(seed: int) -> dict[str, Any]:
    return {
        "config_version": "rules-v1",
        "time_origin_ms": 1700000000000,
        "turn_step_ms": 1000,
        "default_state_generation": 1,
        "scoring": {
            "confidence_weight": 0.7,
            "turn_weight": 0.3,
            "precision": 6,
        },
        "clarification": {
            "threshold": 0.65,
            "pattern": r"\[clarify:([^\]]+)\]",
        },
        "slot_key_pattern": "^[a-z][a-z0-9_.-]{0,63}$",
        "merge": {
            "strategy": "source_turn_then_score_then_value_lexicographic",
        },
        "sample_seed": seed,
    }


def build_sample_turns(seed: int = DEFAULT_SEED) -> tuple[ParsedTurn, ...]:
    rng = np.random.default_rng(seed)
    meal_confidence = round(float(rng.uniform(0.40, 0.60)), 3)
    turns = [
        {
            "session_id": "sess-demo-001",
            "turn_no": 1,
            "user_text": "帮我订杭州的酒店，后天入住",
            "assistant_text": "已记录目的地杭州和入住日期。",
            "slot_candidates": {
                "destination": {"value": "杭州", "confidence": 0.82},
                "checkin_date": {"value": "2026-12-01", "confidence": 0.90},
            },
        },
        {
            "session_id": "sess-demo-001",
            "turn_no": 2,
            "user_text": "目的地改成上海，也想比较一下北京",
            "assistant_text": "已更新目的地候选。",
            "slot_candidates": [
                {"slot_key": "destination", "value": "上海", "confidence": 0.58},
                {"slot_key": "destination", "value": "北京", "confidence": 0.58},
            ],
        },
        {
            "session_id": "sess-demo-001",
            "turn_no": 3,
            "user_text": "两个人住",
            "assistant_text": "请确认目的地和人数 [clarify:destination,guests]",
            "slot_candidates": {
                "guests": {"value": 2, "confidence": 0.55},
            },
        },
        {
            "session_id": "sess-demo-001",
            "turn_no": 4,
            "user_text": "坐高铁去",
            "assistant_text": "请确认交通方式 [clarify:transport]",
            "slot_candidates": {
                "transport": {"value": "高铁", "confidence": 0.52},
            },
        },
        {
            "session_id": "sess-demo-001",
            "turn_no": 5,
            "user_text": "早餐要清淡一点",
            "assistant_text": "请确认早餐偏好 [clarify:meal_preference]",
            "slot_candidates": {
                "meal_preference": {
                    "value": "清淡",
                    "confidence": meal_confidence,
                },
            },
        },
    ]
    from .turns import parse_turn_rows

    return parse_turn_rows(
        turns,
        rules=Rules.from_dict(_rules_payload(seed)),
        source="<sample-generator>",
    )


def generate_samples(
    output_dir: str | Path,
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    output = Path(output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    config_payload = _rules_payload(seed)
    atomic_write_json(output / "config.json", config_payload)
    rules = Rules.from_dict(config_payload, source_path=str(output / "config.json"))
    turns = build_sample_turns(seed)
    turn_lines = [
        canonical_line(turn.raw_row()).encode("utf-8")
        for turn in turns
        if turn.session_id == "sess-demo-001"
    ]
    (output / "turns.jsonl").write_bytes(b"".join(turn_lines))

    with tempfile.TemporaryDirectory(prefix="dialog-state-sample-cache-") as cache_dir:
        engine = DiagnosticEngine(rules, cache_dir)
        result = engine.execute("sess-demo-001", turns)
    v1 = write_v1(
        output / "state_v1.json",
        result.state,
        rules,
        audit_history=result.audit_history,
        include_state_generation=False,
        overwrite=True,
    )
    v2 = write_v2(
        output / "state_v2",
        result.state,
        rules,
        audit_history=result.audit_history,
        overwrite=True,
    )
    v3 = write_v3(
        output / "state_v3.json",
        result.state,
        rules,
        audit_history=result.audit_history,
        overwrite=True,
    )
    manifest = {
        "format": "diagnostic-sample-manifest-v1",
        "seed": seed,
        "session_id": "sess-demo-001",
        "turn_count": len(turns),
        "state_checksum": result.state.state_checksum,
        "state_generation": result.state.state_generation,
        "pending_clarifications": list(result.state.pending_clarifications),
        "formats": {
            "v1": str(Path(v1.path).relative_to(output)),
            "v2": str(Path(v2.path).relative_to(output)),
            "v3": str(Path(v3.path).relative_to(output)),
        },
        "files": {},
    }
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest["files"][str(path.relative_to(output))] = sha256_bytes(
                path.read_bytes()
            )
    atomic_write_json(output / "manifest.json", manifest)
    return manifest

