#!/usr/bin/env python3
"""Generate committed deterministic dialogue samples without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any

SEED = 20261120

SENTIMENT_LEXICON = {
    "good": 0.8,
    "great": 0.9,
    "happy": 0.7,
    "thanks": 0.8,
    "fast": 0.5,
    "bad": -0.8,
    "angry": -1.0,
    "terrible": -1.1,
    "slow": -0.5,
    "disappointed": -0.9,
    "专业": 0.6,
    "及时": 0.6,
    "喜欢": 0.7,
    "感谢": 0.8,
    "满意": 0.9,
    "解决": 0.5,
    "失望": -0.9,
    "生气": -1.0,
    "糟糕": -0.9,
    "太慢": -0.6,
    "差": -0.7,
}

NEGATIONS = ["不是", "并非", "没有", "没", "不", "未", "never", "not", "no"]
DEGREE_ADVERBS = {
    "极其": 1.8,
    "非常": 1.6,
    "特别": 1.5,
    "太": 1.4,
    "really": 1.3,
    "很": 1.2,
    "very": 1.5,
    "slightly": 0.6,
    "有点": 0.7,
    "稍微": 0.6,
}

RISK_TERMS = {
    "abuse": [
        {"term": "蠢货", "severity": 0.7},
        {"term": "废物", "severity": 0.6},
        {"term": "滚", "severity": 0.4},
        {"term": "idiot", "severity": 0.7},
        {"term": "stupid", "severity": 0.6},
        {"term": "useless", "severity": 0.5},
    ],
    "fraud": [
        {"term": "转账", "severity": 0.8},
        {"term": "验证码", "severity": 0.9},
        {"term": "退款链接", "severity": 0.8},
        {"term": "汇款", "severity": 0.8},
        {"term": "刷单", "severity": 0.7},
        {"term": "gift card", "severity": 0.8},
        {"term": "wire transfer", "severity": 0.9},
    ],
    "privacy": [
        {"term": "身份证", "severity": 0.6},
        {"term": "银行卡号", "severity": 0.8},
        {"term": "家庭住址", "severity": 0.6},
        {"term": "密码", "severity": 0.9},
        {"term": "id card", "severity": 0.6},
        {"term": "home address", "severity": 0.6},
        {"term": "password", "severity": 0.9},
    ],
    "safety": [
        {"term": "炸弹", "severity": 1.0},
        {"term": "自杀", "severity": 1.0},
        {"term": "威胁", "severity": 0.8},
        {"term": "弄死", "severity": 1.0},
        {"term": "bomb", "severity": 1.0},
        {"term": "suicide", "severity": 1.0},
        {"term": "threat", "severity": 0.8},
    ],
}

LABELS = {
    "positive": [
        "你们的客服很及时，我很满意",
        "问题已经解决了，非常感谢",
        "这次处理得很专业，我很喜欢",
        "The service was great and fast, thanks",
        "Order resolved, I am happy with the support",
    ],
    "negative": [
        "我非常失望，处理速度太慢",
        "我很生气，这个结果太糟糕",
        "不太满意，你们并没有解决",
        "I am really disappointed, this is terrible",
        "The process was bad and very slow",
    ],
    "neutral": [
        "我想咨询一下订单状态",
        "请问还需要补充什么信息",
        "Could you check the order for me?",
        "Please confirm the delivery window",
    ],
}

RISK_LINES = {
    "abuse": [
        "你这个蠢货，赶紧处理",
        "真是废物，快滚",
        "You useless idiot, stop replying",
    ],
    "fraud": [
        "对方让我提供验证码并转账，这是退款链接",
        "请先做一笔汇款，再点这个刷单链接",
        "Send a gift card code or use wire transfer now",
    ],
    "privacy": [
        "请提供身份证和银行卡号，还有家庭住址",
        "把你的密码发给我核对",
        "Send your id card, home address and password",
    ],
    "safety": [
        "如果你不处理，我就威胁你",
        "再这样我就弄死你",
        "There is a bomb threat in this message",
    ],
}

AGENT_LINES = [
    "您好，我来协助处理，请稍等",
    "已经记录您的问题，正在核实",
    "抱歉给您带来不便，我们会尽快解决",
    "I have recorded the issue and am checking it",
    "Thanks for the details, we will follow up",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "config_version": 1,
        "versions": {"results": 1, "weights": 1},
        "precision": {"decimals": 6, "flag": "decimal_6"},
        "sentiment": {
            "label_order": ["negative", "neutral", "positive"],
            "neutral_baseline": 0.18,
            "category_weights": {
                "negative": 1.0,
                "neutral": 1.0,
                "positive": 1.0,
            },
            "lexicon": dict(sorted(SENTIMENT_LEXICON.items())),
        },
        "context": {
            "negations": sorted(NEGATIONS, key=lambda term: (-len(term), term)),
            "degree_adverbs": dict(sorted(DEGREE_ADVERBS.items())),
            "lookback_codepoints": 8,
        },
        "aggregation": {
            "decay": {"enabled": True, "half_life_steps": 4.0},
            "risk_score_cap": 1.0,
            "risk_category_weights": {
                "abuse": 0.8,
                "fraud": 1.0,
                "privacy": 0.9,
                "safety": 1.2,
            },
            "risk_levels": [
                {"label": "critical", "min_score": 0.65},
                {"label": "high", "min_score": 0.4},
                {"label": "medium", "min_score": 0.15},
                {"label": "low", "min_score": 0.0},
            ],
        },
        "batch_size": 4,
        "cache": {
            "enabled": True,
            "max_entries": 128,
            "invalidation": "affected_conversations_only",
        },
        "resource_limits": {
            "max_conversations": 50,
            "max_messages": 1000,
            "max_cache_entries": 128,
        },
        "query": {"latency_budget_ms": 50.0},
    }


def build_risk_words() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "version": 1,
        "categories": ["abuse", "fraud", "privacy", "safety"],
        "terms": {
            category: sorted(entries, key=lambda entry: entry["term"])
            for category, entries in sorted(RISK_TERMS.items())
        },
    }


def build_conversations(rng: random.Random) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    locales = ["zh-CN", "en-US"]
    groups = ["A", "B"]
    weights = [1.0, 1.0, 1.0, 1.2, 0.8]
    risk_categories = sorted(RISK_LINES)

    for conv_no in range(1, 9):
        conv_id = f"conv-{conv_no:04d}"
        locale = locales[(conv_no - 1) % len(locales)]
        group = groups[(conv_no - 1) % len(groups)]
        message_count = 6 + (conv_no % 3)
        risk_slots: dict[int, str] = {}
        if conv_no != 6:
            risk_slots[2 + (conv_no % 3)] = risk_categories[(conv_no - 1) % len(risk_categories)]
        if conv_no in (4, 7):
            risk_slots[1] = risk_categories[(conv_no + 1) % len(risk_categories)]

        for msg_no in range(1, message_count + 1):
            speaker = "customer" if msg_no % 2 == 1 else "agent"
            if msg_no in risk_slots:
                category = risk_slots[msg_no]
                text = rng.choice(RISK_LINES[category])
                speaker = "customer"
            elif speaker == "agent":
                text = rng.choice(AGENT_LINES)
            else:
                label = ("positive", "neutral", "negative")[
                    (conv_no + msg_no) % 3
                ]
                text = rng.choice(LABELS[label])
            records.append(
                {
                    "annotator_group": group,
                    "conv_id": conv_id,
                    "locale": locale,
                    "msg_no": msg_no,
                    "speaker": speaker,
                    "text": text,
                    "weight": weights[(conv_no + msg_no) % len(weights)],
                }
            )
    return records


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(out_dir: Path, seed: int = SEED) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    config = build_config()
    risk_words = build_risk_words()
    records = build_conversations(random.Random(seed))

    conversation_path = out_dir / "conversations.jsonl"
    conversation_path.write_text(
        "".join(_canonical(record) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )
    _write_json(out_dir / "config.json", config)
    _write_json(out_dir / "risk_words.json", risk_words)
    metadata = {
        "schema_version": 1,
        "seed": seed,
        "generator": "scripts/generate_samples.py",
        "files": {
            "conversations.jsonl": _sha256(conversation_path),
            "config.json": _sha256(out_dir / "config.json"),
            "risk_words.json": _sha256(out_dir / "risk_words.json"),
        },
        "conversation_count": len({record["conv_id"] for record in records}),
        "message_count": len(records),
    }
    _write_json(out_dir / "sample_meta.json", metadata)
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data",
        help="output directory (default: repository data/)",
    )
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    metadata = generate(args.out, args.seed)
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

