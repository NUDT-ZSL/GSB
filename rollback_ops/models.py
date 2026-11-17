"""内部归一模型：变更单、变更项、回滚计划。所有模型不可变且可稳定比较。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

CURRENT_FORMAT_VERSION = 3
SUPPORTED_FORMAT_VERSIONS = (1, 2, 3)
SUPPORTED_CATEGORIES = ("application", "database", "network", "os", "security")

# 低版本归一时的确定性默认值
DEFAULT_CATEGORY_V1 = "application"
DEFAULT_SOURCE_SYSTEM = {1: "legacy-v1", 2: "legacy-v2"}
DEFAULT_RISK_WEIGHT = 1


def canonical_json(obj) -> str:
    """稳定序列化：键排序、紧凑分隔符，用于比较与指纹。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChangeItem:
    key: str
    old_value: object
    new_value: object
    category: str
    impacted_services: tuple  # 已排序的 str 元组
    source_system: str
    risk_weight: int
    source_format_version: int  # 来源格式版本标记 1|2|3

    def content_dict(self):
        return {
            "key": self.key,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "category": self.category,
            "impacted_services": list(self.impacted_services),
            "source_system": self.source_system,
            "risk_weight": self.risk_weight,
        }

    def to_dict(self):
        d = self.content_dict()
        d["source_format_version"] = self.source_format_version
        return d

    def fingerprint(self) -> str:
        """内容指纹（不含来源版本标记），用于增量缓存校验。"""
        return fingerprint_of(self.content_dict())


@dataclass(frozen=True)
class NormalizedTicket:
    ticket_id: str
    revision: int                    # 变更单版本号，演进时单调递增
    source_format_version: int       # 来源格式版本 1|2|3
    items: tuple                     # ChangeItem 元组，按 key 排序

    def to_dict(self):
        return {
            "ticket_id": self.ticket_id,
            "revision": self.revision,
            "source_format_version": self.source_format_version,
            "items": [it.to_dict() for it in self.items],
        }

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def fingerprint(self) -> str:
        return fingerprint_of(self.to_dict())

    def item_map(self):
        return {it.key: it for it in self.items}

    def __eq__(self, other):
        return isinstance(other, NormalizedTicket) and self.canonical_json() == other.canonical_json()

    def __hash__(self):
        return hash(self.canonical_json())


@dataclass(frozen=True)
class RollbackStep:
    step_id: str
    change_key: str
    action: str
    depends_on: tuple  # 已排序的 step_id 元组

    def to_dict(self):
        return {
            "step_id": self.step_id,
            "change_key": self.change_key,
            "action": self.action,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class RollbackPlan:
    plan_version: int
    steps: tuple  # RollbackStep 元组，按 step_id 排序

    def to_dict(self):
        return {
            "plan_version": self.plan_version,
            "steps": [s.to_dict() for s in self.steps],
        }

    def fingerprint(self) -> str:
        return fingerprint_of(self.to_dict())

    def step_map(self):
        return {s.step_id: s for s in self.steps}
