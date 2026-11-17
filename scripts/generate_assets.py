#!/usr/bin/env python3
"""Generate the fixed-seed local samples, rule pack, config, and fixtures."""

from __future__ import annotations

import hashlib
import json
import random
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEED = 20261117
SAMPLE_COUNT = 600


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def build_rules() -> dict:
    return {
        "schema_version": 1,
        "sentiment_lexicon": [
            {"word": "满意", "weight": 0.82},
            {"word": "高兴", "weight": 0.72},
            {"word": "感谢", "weight": 0.65},
            {"word": "放心", "weight": 0.60},
            {"word": "喜欢", "weight": 0.70},
            {"word": "顺利", "weight": 0.55},
            {"word": "专业", "weight": 0.50},
            {"word": "贴心", "weight": 0.68},
            {"word": "解决", "weight": 0.45},
            {"word": "认可", "weight": 0.58},
            {"word": "不满意", "weight": -0.90},
            {"word": "失望", "weight": -0.85},
            {"word": "生气", "weight": -0.80},
            {"word": "担心", "weight": -0.55},
            {"word": "着急", "weight": -0.60},
            {"word": "愤怒", "weight": -0.95},
            {"word": "糟糕", "weight": -0.88},
            {"word": "延误", "weight": -0.50},
            {"word": "敷衍", "weight": -0.72},
        ],
        "degree_adverbs": [
            {"word": "非常", "factor": 1.80},
            {"word": "很", "factor": 1.50},
            {"word": "特别", "factor": 2.00},
            {"word": "有点", "factor": 0.70},
            {"word": "太", "factor": 1.60},
            {"word": "稍微", "factor": 0.80},
        ],
        "negation_words": ["不", "没", "没有", "未", "别"],
        "risk_keywords": [
            {"keyword": "咨询", "level": "一般", "weight": 0.35},
            {"keyword": "了解", "level": "一般", "weight": 0.30},
            {"keyword": "请问", "level": "一般", "weight": 0.28},
            {"keyword": "查询", "level": "一般", "weight": 0.32},
            {"keyword": "催一下", "level": "催办", "weight": 0.62},
            {"keyword": "尽快", "level": "催办", "weight": 0.66},
            {"keyword": "什么时候", "level": "催办", "weight": 0.58},
            {"keyword": "多久", "level": "催办", "weight": 0.52},
            {"keyword": "加急", "level": "催办", "weight": 0.68},
            {"keyword": "投诉", "level": "投诉", "weight": 0.75},
            {"keyword": "差评", "level": "投诉", "weight": 0.78},
            {"keyword": "退费", "level": "投诉", "weight": 0.64},
            {"keyword": "主管", "level": "投诉", "weight": 0.55},
            {"keyword": "服务态度", "level": "投诉", "weight": 0.70},
            {"keyword": "起诉", "level": "风险", "weight": 0.90},
            {"keyword": "曝光", "level": "风险", "weight": 0.92},
            {"keyword": "人身安全", "level": "风险", "weight": 1.00},
            {"keyword": "媒体", "level": "风险", "weight": 0.84},
            {"keyword": "监管", "level": "风险", "weight": 0.88},
            {"keyword": "报警", "level": "风险", "weight": 0.96},
        ],
        "risk_level_map": {
            "一般": {"parent": None, "weight": 0.25},
            "催办": {"parent": "一般", "weight": 0.55},
            "投诉": {"parent": "催办", "weight": 0.85},
            "风险": {"parent": "投诉", "weight": 1.25},
        },
        "conflict_rules": [
            {
                "sentiment": "positive",
                "risk_level": "催办",
                "winner": "risk",
                "resolved_level": "催办",
                "reason": "urgency_outweighs_positive_service_tone",
            },
            {
                "sentiment": "positive",
                "risk_level": "投诉",
                "winner": "risk",
                "resolved_level": "投诉",
                "reason": "complaint_signal_outweighs_positive_tone",
            },
            {
                "sentiment": "positive",
                "risk_level": "风险",
                "winner": "risk",
                "resolved_level": "风险",
                "reason": "high_risk_signal_outweighs_positive_tone",
            },
            {
                "sentiment": "negative",
                "risk_level": "一般",
                "winner": "sentiment",
                "resolved_level": "催办",
                "reason": "negative_tone_escalates_general_ticket",
            },
        ],
    }


def build_config() -> dict:
    return {
        "schema_version": 1,
        "channels": ["在线客服", "电话工单", "应用内", "邮件"],
        "lexicon_weight_scale": 1.0,
        "risk_weight_scale": 1.0,
        "position_decay": 0.20,
        "negation_window": 4,
        "risk_level_thresholds": {"一般": 0.08, "催办": 0.25, "投诉": 0.45, "风险": 0.65},
        "fusion_temperature": 0.35,
        "interaction_budget_ms": 200.0,
        "worker_threads": 2,
        "snapshot_commit_interval": 5,
    }


POSITIVE_WORDS = ["满意", "高兴", "感谢", "放心", "喜欢", "顺利", "专业", "贴心", "认可"]
NEGATIVE_WORDS = ["不满意", "失望", "生气", "担心", "着急", "愤怒", "糟糕", "延误", "敷衍"]
DEGREES = ["非常", "很", "特别", "有点", "太", "稍微"]
CHANNELS = ["在线客服", "电话工单", "应用内", "邮件"]


def build_template_bank() -> dict[str, dict[str, list[str]]]:
    return {
        "一般": {
            "positive": [
                "我想咨询一下进度，{degree}{positive}，流程也{positive2}",
                "请问如何查询记录，目前处理得{positive}，我很{positive2}",
                "了解一下这个功能，答复很{positive}，让人{positive2}",
            ],
            "negative": [
                "我想咨询退款规则，但是处理有点{negative}，希望说明",
                "请问为什么还没有解决，我有点{negative}，请查询一下",
                "想了解进度，结果让我{negative}，回复也{negative2}",
            ],
        },
        "催办": {
            "positive": [
                "麻烦尽快处理，之前沟通很{positive}，谢谢你们{positive2}",
                "请催一下进度，整体服务{positive}，我还是{positive2}的",
                "加急看一下可以吗，专员很{positive}，我很{positive2}",
            ],
            "negative": [
                "请尽快回复，已经等很久了，我很{negative}，别再{negative2}",
                "催一下什么时候能解决，迟迟没有进展，真的{negative}",
                "加急处理一下吧，现状太{negative}，我非常{negative2}",
            ],
        },
        "投诉": {
            "positive": [
                "我要投诉这个流程，不过客服态度很{positive}，请主管核实",
                "准备给差评，但专员回复很{positive}，希望尽快改进",
                "申请退费并投诉服务态度，之前体验其实还算{positive}",
            ],
            "negative": [
                "我要投诉，服务态度太{negative}，请主管马上处理",
                "这次体验非常{negative}，我要给差评并要求退费",
                "主管请核实，客服处理太{negative}，我特别{negative2}",
            ],
        },
        "风险": {
            "positive": [
                "如果今天还不解决我就起诉，不过之前沟通还算{positive}",
                "准备向监管投诉并曝光，之前专员其实很{positive}",
                "涉及人身安全，我会报警，尽快处理后我还是{positive}的",
            ],
            "negative": [
                "再不处理我就起诉并曝光，对此我非常{negative}",
                "我要向媒体和监管反映，现状太{negative}，我很{negative2}",
                "问题涉及人身安全，我会报警，处理过程太{negative}",
            ],
        },
    }


def build_samples() -> list[dict]:
    rng = random.Random(SEED)
    bank = build_template_bank()
    levels = ["一般", "催办", "投诉", "风险"]
    samples: list[dict] = []
    for index in range(SAMPLE_COUNT):
        level = levels[index % len(levels)]
        sentiment = "positive" if (index // len(levels)) % 2 == 0 else "negative"
        template = rng.choice(bank[level][sentiment])
        positive = rng.choice(POSITIVE_WORDS)
        positive2 = rng.choice([word for word in POSITIVE_WORDS if word != positive])
        negative = rng.choice(NEGATIVE_WORDS)
        negative2 = rng.choice([word for word in NEGATIVE_WORDS if word != negative])
        degree = rng.choice(DEGREES)
        text = template.format(
            degree=degree,
            positive=positive,
            positive2=positive2,
            negative=negative,
            negative2=negative2,
        )
        samples.append(
            {
                "id": f"T{index + 1:04d}",
                "text": text,
                "channel": rng.choice(CHANNELS),
            }
        )
    return samples


def main() -> None:
    rules = build_rules()
    config = build_config()
    samples = build_samples()

    write_json(ROOT / "resources" / "rules.json", rules)
    write_json(ROOT / "config" / "determination.json", config)
    sample_path = ROOT / "data" / "samples.jsonl"
    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_text(
        "".join(canonical(sample) + "\n" for sample in samples), encoding="utf-8"
    )

    replay = """# 固定编辑回放：权重、阈值、词条、程度词与否定窗口
set_lexicon 满意 0.95
set_threshold 风险 0.62
set_weight fusion_temperature 0.42
set_risk_keyword 投诉 0.82
set_lexicon 失望 -0.95
set_weight risk_scale 1.08
set_threshold 投诉 0.42
set_degree 非常 1.90
set_negation_window 5
set_risk_level_weight 催办 0.60
set_threshold 催办 0.24
set_weight sentiment_scale 1.03
set_weight position_decay 0.16
wait
verify
snapshot
report
quit
"""
    (ROOT / "commands" / "replay.txt").write_text(replay, encoding="utf-8")

    invalid = [
        '{"id":"BAD01","text":"   ","channel":"在线客服"}',
        'this is not json',
        '{"id":"BAD02","text":"测试句子","channel":"未知渠道"}',
        '{"id":"OK01","text":"合法文本","channel":"电话工单"}',
        '{"id":"OK01","text":"重复 ID","channel":"在线客服"}',
        '{"id":"OK02","text":"另一条合法文本","channel":"邮件"}',
    ]
    (ROOT / "tests" / "fixtures" / "invalid_samples.jsonl").write_text(
        "\n".join(invalid) + "\n", encoding="utf-8"
    )

    cyclic = json.loads(json.dumps(rules))
    cyclic["risk_level_map"]["一般"]["parent"] = "风险"
    write_json(ROOT / "tests" / "fixtures" / "cycle_rules.json", cyclic)

    outputs = [
        ROOT / "data" / "samples.jsonl",
        ROOT / "resources" / "rules.json",
        ROOT / "config" / "determination.json",
        ROOT / "commands" / "replay.txt",
        ROOT / "tests" / "fixtures" / "invalid_samples.jsonl",
        ROOT / "tests" / "fixtures" / "cycle_rules.json",
    ]
    meta = {
        "seed": SEED,
        "sample_count": len(samples),
        "files": {str(path.relative_to(ROOT)): sha256(path) for path in outputs},
    }
    write_json(ROOT / "assets_meta.json", meta)


if __name__ == "__main__":
    main()
