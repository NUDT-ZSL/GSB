from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .config import ConfigBundle, migrate_config, write_v1
from .util import atomic_write_json, atomic_write_text


DOMAIN = "finance"
LOCALE = "zh-CN"
DICTIONARY_VERSION = "lex-2026-11-27.1"
RULE_VERSION = "rules-2026-11-27.1"
TAGS = [
    "NOUN",
    "VERB",
    "ADJ",
    "ADV",
    "PART",
    "PREP",
    "NUM",
    "ORG",
    "MONEY",
    "PERCENT",
    "DATE",
    "PUNCT",
    "UNK",
    "WS",
]


def _entry(entry_id: str, term: str, tag: str) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "term": term,
        "tag": tag,
        "version": DICTIONARY_VERSION,
        "domains": [DOMAIN],
        "locales": [LOCALE],
    }


def _rule(
    rule_id: str,
    pattern: str,
    source: str,
    tag: str,
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "pattern": pattern,
        "source": source,
        "tag": tag,
        "version": RULE_VERSION,
        "domains": [DOMAIN],
        "locales": [LOCALE],
    }


def default_config_data() -> dict[str, Any]:
    entries = [
        _entry("lex-001", "支付宝", "ORG"),
        _entry("lex-002", "蚂蚁集团", "ORG"),
        _entry("lex-003", "网络技术", "NOUN"),
        _entry("lex-004", "alipay", "ORG"),
        _entry("lex-005", "发布", "VERB"),
        _entry("lex-006", "了", "PART"),
        _entry("lex-007", "第三季度", "DATE"),
        _entry("lex-008", "财报", "NOUN"),
        _entry("lex-009", "向", "PREP"),
        _entry("lex-010", "供应商", "NOUN"),
        _entry("lex-011", "货款", "NOUN"),
        _entry("lex-012", "跨境", "ADJ"),
        _entry("lex-013", "业务", "NOUN"),
        _entry("lex-014", "增长", "VERB"),
        _entry("lex-015", "合规", "NOUN"),
        _entry("lex-016", "审计", "NOUN"),
        _entry("lex-017", "通过", "VERB"),
        _entry("lex-018", "用户", "NOUN"),
        _entry("lex-019", "使用", "VERB"),
        _entry("lex-020", "完成", "VERB"),
        _entry("lex-021", "结算", "NOUN"),
        _entry("lex-022", "电子", "ADJ"),
        _entry("lex-023", "发票", "NOUN"),
        _entry("lex-024", "已", "ADV"),
        _entry("lex-025", "开具", "VERB"),
        _entry("lex-026", "请", "VERB"),
        _entry("lex-027", "查收", "VERB"),
        _entry("lex-028", "第一季度", "DATE"),
        _entry("lex-029", "营收", "NOUN"),
        _entry("lex-030", "同比", "ADV"),
        _entry("lex-031", "第二季度", "DATE"),
        _entry("lex-032", "订单", "NOUN"),
        _entry("lex-033", "退款", "NOUN"),
        _entry("lex-034", "审核", "VERB"),
        _entry("lex-035", "系统", "NOUN"),
        _entry("lex-036", "处理", "VERB"),
        _entry("lex-037", "收款", "VERB"),
        _entry("lex-038", "服务费", "NOUN"),
        _entry("lex-039", "人民币", "NOUN"),
        _entry("lex-040", "账户", "NOUN"),
    ]
    rules = [
        _rule(
            "rule-money",
            r"\d+(?:\.\d+)?(?:亿元|万元|元)",
            "prefix_rule",
            "MONEY",
        ),
        _rule("rule-year", r"\d{4}年", "prefix_rule", "DATE"),
        _rule(
            "rule-percent",
            r"\d+(?:\.\d+)?%",
            "prefix_rule",
            "PERCENT",
        ),
        _rule("rule-payment", r"支付", "prefix_rule", "VERB"),
        _rule("rule-company-suffix", r"有限公司", "suffix_rule", "ORG"),
    ]
    domain_pos_table = {
        DOMAIN: {
            "。": {"PUNCT": 500.0},
            "，": {"PUNCT": 500.0},
            ",": {"PUNCT": 500.0},
            ".": {"PUNCT": 500.0},
            "%": {"PUNCT": 500.0},
            "?": {"PUNCT": 500.0},
            "!": {"PUNCT": 500.0},
            ";": {"PUNCT": 500.0},
            ":": {"PUNCT": 500.0},
            "、": {"PUNCT": 500.0},
            "；": {"PUNCT": 500.0},
            "：": {"PUNCT": 500.0},
            "？": {"PUNCT": 500.0},
            "！": {"PUNCT": 500.0},
            "★": {"UNK": 500.0},
            " ": {"WS": 500.0},
        }
    }
    return {
        "format_version": 1,
        "locale": LOCALE,
        "domain": DOMAIN,
        "dictionary_version": DICTIONARY_VERSION,
        "rule_version": RULE_VERSION,
        "workers": 2,
        "batch_size": 4,
        "logical_clock_ms": 1764201600000,
        "generation": 1,
        "normalization": {
            "unicode_form": "NFKC",
            "case_mode": "casefold",
            "whitespace_mode": "collapse",
            "domain_replacements": {},
        },
        "disambiguation": {
            "priority": {
                "dictionary": 300,
                "prefix_rule": 200,
                "suffix_rule": 100,
            },
            "tie_break": ["shorter", "codepoint_asc"],
        },
        "dictionary": entries,
        "rules": rules,
        "domain_tags": {DOMAIN: TAGS},
        "domain_pos_table": domain_pos_table,
        "tagger": {
            "initial": {tag: 1.0 for tag in TAGS},
            "transitions": {
                tag: {next_tag: 1.0 for next_tag in TAGS} for tag in TAGS
            },
            "default_emission": 0.01,
            "dictionary_emission": 50.0,
            "unknown_emission": 50.0,
        },
        "limits": {
            "max_batch_sentences": 32,
            "max_workers": 8,
            "max_token_cache_bytes": 4 * 1024 * 1024,
            "estimated_bytes_per_token": 64,
            "resource_policy": "degrade_serial",
        },
    }


TEMPLATES: list[tuple[str, list[str], list[str]]] = [
    (
        "支付宝发布了2025年第三季度财报。",
        ["支付宝", "发布", "了", "2025年", "第三季度", "财报", "。"],
        ["ORG", "VERB", "PART", "DATE", "DATE", "NOUN", "PUNCT"],
    ),
    (
        "蚂蚁集团向供应商支付了380万元货款。",
        ["蚂蚁集团", "向", "供应商", "支付", "了", "380万元", "货款", "。"],
        ["ORG", "PREP", "NOUN", "VERB", "PART", "MONEY", "NOUN", "PUNCT"],
    ),
    (
        "跨境支付业务增长12.5%，合规审计通过。",
        [
            "跨境",
            "支付",
            "业务",
            "增长",
            "12.5%",
            ",",
            "合规",
            "审计",
            "通过",
            "。",
        ],
        [
            "ADJ",
            "VERB",
            "NOUN",
            "VERB",
            "PERCENT",
            "PUNCT",
            "NOUN",
            "NOUN",
            "VERB",
            "PUNCT",
        ],
    ),
    (
        "用户使用Ａｌｉｐａｙ完成结算。",
        ["用户", "使用", "alipay", "完成", "结算", "。"],
        ["NOUN", "VERB", "ORG", "VERB", "NOUN", "PUNCT"],
    ),
    (
        "电子发票已开具，请查收。",
        ["电子", "发票", "已", "开具", ",", "请", "查收", "。"],
        ["ADJ", "NOUN", "ADV", "VERB", "PUNCT", "VERB", "VERB", "PUNCT"],
    ),
    (
        "2026年第一季度营收同比增长8.2%。",
        ["2026年", "第一季度", "营收", "同比", "增长", "8.2%", "。"],
        ["DATE", "DATE", "NOUN", "ADV", "VERB", "PERCENT", "PUNCT"],
    ),
    (
        "支付宝  发布了 财报。",
        ["支付宝", " ", "发布", "了", " ", "财报", "。"],
        ["ORG", "WS", "VERB", "PART", "WS", "NOUN", "PUNCT"],
    ),
    (
        "蚂蚁集团网络技术有限公司处理订单★",
        ["蚂蚁集团", "网络技术", "有限公司", "处理", "订单", "★"],
        ["ORG", "NOUN", "ORG", "VERB", "NOUN", "UNK"],
    ),
]


def generate_tasks(seed: int, count: int = 24) -> list[dict[str, Any]]:
    if count < 1:
        raise ValueError("count must be positive")
    rng = random.Random(seed)
    tasks: list[dict[str, Any]] = []
    for index in range(count):
        raw_text, token_list, pos_tag_list = TEMPLATES[
            rng.randrange(len(TEMPLATES))
        ]
        tasks.append(
            {
                "sentence_id": f"s{index + 1:04d}",
                "raw_text": raw_text,
                "token_list": list(token_list),
                "pos_tag_list": list(pos_tag_list),
                "lang": LOCALE,
                "domain": DOMAIN,
            }
        )
    return tasks


def generate_sample(
    output_dir: str | Path,
    *,
    seed: int = 20261127,
    count: int = 24,
) -> dict[str, Any]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    config_data = default_config_data()
    bundle = ConfigBundle(
        data=config_data,
        format_version=1,
        source="generated:sample",
        provenance={
            key: "generated:sample-defaults"
            for key in (
                "dictionary_version",
                "rule_version",
                "workers",
                "batch_size",
                "logical_clock_ms",
                "generation",
            )
        },
    )
    v1_path = root / "config" / "v1.json"
    write_v1(bundle, v1_path)
    v2_path = root / "config" / "v2"
    if v2_path.exists():
        for path in sorted(v2_path.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    migrate_config(v1_path, v2_path, 2, overwrite=True)
    tasks = generate_tasks(seed, count)
    tasks_path = root / "tasks.jsonl"
    atomic_write_text(
        tasks_path,
        "".join(
            json.dumps(task, ensure_ascii=False, sort_keys=True) + "\n"
            for task in tasks
        ),
    )
    metadata = {
        "seed": seed,
        "count": count,
        "domain": DOMAIN,
        "locale": LOCALE,
        "dictionary_version": DICTIONARY_VERSION,
        "rule_version": RULE_VERSION,
        "v1_config": str(v1_path),
        "v2_config": str(v2_path),
        "tasks": str(tasks_path),
    }
    atomic_write_json(root / "sample_manifest.json", metadata)
    return metadata

