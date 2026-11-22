"""数据模型与确定性序列化工具。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


def sha256_hex(payload: str) -> str:
    """对字符串做 UTF-8 编码后的 sha256 十六进制摘要。"""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_json(obj: object) -> str:
    """键排序、无多余空白、不转义非 ASCII 的规范化 JSON 文本。

    用于内容校验和与内容寻址键，保证跨机器逐字节一致。
    """
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def codepoint_len(text: str) -> int:
    """码点长度（Python str 长度即 Unicode 码点数）。"""
    return len(text)


@dataclass(frozen=True)
class Ticket:
    """一条本地确定性生成的客服工单。"""

    ticket_id: str
    query_text: str
    locale: str
    intent_label_gold: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        data: Dict[str, object] = {
            "ticket_id": self.ticket_id,
            "query_text": self.query_text,
            "locale": self.locale,
            "intent_label_gold": self.intent_label_gold,
        }
        return data

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "Ticket":
        return Ticket(
            ticket_id=str(data["ticket_id"]),
            query_text=str(data["query_text"]),
            locale=str(data["locale"]),
            intent_label_gold=(
                None if data.get("intent_label_gold") is None
                else str(data["intent_label_gold"])
            ),
        )


@dataclass(frozen=True)
class IntentRule:
    """单个意图的规则声明。"""

    label: str
    keywords: Tuple[str, ...]
    weight: int
    threshold: float
    fallback_label: Optional[str]
    batch_size: int

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "IntentRule":
        return IntentRule(
            label=str(data["label"]),
            keywords=tuple(str(k) for k in data["keywords"]),
            weight=int(data["weight"]),
            threshold=float(data["threshold"]),
            fallback_label=(
                None if data.get("fallback_label") is None
                else str(data["fallback_label"])
            ),
            batch_size=int(data["batch_size"]),
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "label": self.label,
            "keywords": list(self.keywords),
            "weight": self.weight,
            "threshold": self.threshold,
            "fallback_label": self.fallback_label,
            "batch_size": self.batch_size,
        }


@dataclass(frozen=True)
class IntentConfig:
    """意图规则配置：受控标签集合 + 规则 + 版本元数据。"""

    version: int
    controlled_labels: Tuple[str, ...]
    rules: Tuple[IntentRule, ...]
    default_fallback_label: str
    rule_version: str
    threshold_version: str
    checksum: str

    @staticmethod
    def build(
        version: int,
        controlled_labels: Sequence[str],
        rules: Sequence[IntentRule],
        default_fallback_label: str,
    ) -> "IntentConfig":
        """根据规则内容派生规则版本、门限版本与整体校验和。"""
        rule_corpus = {
            r.label: sorted(set(r.keywords))
            for r in sorted(rules, key=lambda r: r.label)
        }
        threshold_corpus = {
            r.label: r.threshold
            for r in sorted(rules, key=lambda r: r.label)
        }
        rule_version = sha256_hex(canonical_json(rule_corpus))[:16]
        threshold_version = sha256_hex(canonical_json(threshold_corpus))[:16]
        checksum = sha256_hex(
            canonical_json(
                {
                    "version": version,
                    "controlled_labels": list(controlled_labels),
                    "rules": [r.to_dict() for r in rules],
                    "default_fallback_label": default_fallback_label,
                }
            )
        )
        return IntentConfig(
            version=version,
            controlled_labels=tuple(controlled_labels),
            rules=tuple(rules),
            default_fallback_label=default_fallback_label,
            rule_version=rule_version,
            threshold_version=threshold_version,
            checksum=checksum,
        )

    def to_dict(self) -> Dict[str, object]:
        return {
            "version": self.version,
            "controlled_labels": list(self.controlled_labels),
            "rules": [r.to_dict() for r in self.rules],
            "default_fallback_label": self.default_fallback_label,
            "rule_version": self.rule_version,
            "threshold_version": self.threshold_version,
            "checksum": self.checksum,
        }

    @staticmethod
    def from_dict(data: Dict[str, object]) -> "IntentConfig":
        return IntentConfig(
            version=int(data["version"]),
            controlled_labels=tuple(str(x) for x in data["controlled_labels"]),
            rules=tuple(IntentRule.from_dict(r) for r in data["rules"]),
            default_fallback_label=str(data["default_fallback_label"]),
            rule_version=str(data["rule_version"]),
            threshold_version=str(data["threshold_version"]),
            checksum=str(data["checksum"]),
        )

    def rule_map(self) -> Dict[str, IntentRule]:
        return {r.label: r for r in self.rules}

    def all_keywords(self) -> List[Tuple[str, str]]:
        """返回去重后的 (label, keyword) 列表，按标签、关键词排序。"""
        pairs = {(r.label, kw) for r in self.rules for kw in r.keywords}
        return sorted(pairs, key=lambda p: (p[0], p[1]))
