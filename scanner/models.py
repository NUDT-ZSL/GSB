"""统一字段定义与数据模型。

设计约束：
- 严重级别仅允许 高 / 中 / 低 三档；
- 结论仅允许 已确认暴露 / 可疑 / 未完成 / 合规 四类；
- 所有缺失值一律使用 None（序列化为 null），禁止空字符串占位。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ---- 枚举（中文取值，确定性排列顺序在此集中定义） ----

SEVERITY_HIGH = "高"
SEVERITY_MEDIUM = "中"
SEVERITY_LOW = "低"
SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}

CONCLUSION_CONFIRMED = "已确认暴露"
CONCLUSION_SUSPECTED = "可疑"
CONCLUSION_INCOMPLETE = "未完成"
CONCLUSION_CLEAN = "合规"

STATUS_PRESENT = "未消除"
STATUS_REMOVED = "已消除"
STATUS_NEW = "新增"


@dataclass(frozen=True)
class Finding:
    """单条暴露/可疑记录（同一样本内按 dedup_key 合并后生成）。"""

    finding_id: str
    category: str  # 暴露类别
    severity: str  # 高 | 中 | 低
    conclusion: str  # 已确认暴露 | 可疑
    location: str  # 证据位置的人类可读定位
    field_name: str | None  # 涉及的头名称/字段名，不适用时为 None
    evidence: str  # 已脱敏的证据描述，不回显完整敏感值
    occurrences: int  # 同一暴露在同一样本中的出现次数
    remediation: str | None  # 处置说明，仅描述应移除/替换的内容，无建议时为 None
    dedup_key: str = field(repr=False, compare=False)


@dataclass
class SampleResult:
    """单个样本的检测结论。"""

    sample_id: str
    sample_file: str
    status: str  # 完成 | 未完成
    conclusion: str  # 已确认暴露 | 可疑 | 未完成 | 合规
    error: str | None
    findings: list[Finding]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "sample_file": self.sample_file,
            "status": self.status,
            "conclusion": self.conclusion,
            "error": self.error,
            "findings": [
                {
                    "finding_id": f.finding_id,
                    "category": f.category,
                    "severity": f.severity,
                    "conclusion": f.conclusion,
                    "location": f.location,
                    "field_name": f.field_name,
                    "evidence": f.evidence,
                    "occurrences": f.occurrences,
                    "remediation": f.remediation,
                }
                for f in self.findings
            ],
        }
