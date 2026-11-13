"""数据模型：问题记录与规则元数据。"""
from __future__ import annotations

import dataclasses

#: 规则标识 -> 一句话说明
RULE_DESCRIPTIONS: dict[str, str] = {
    "SEC001": "外部输入经字符串拼接后直接用于路径打开调用，存在越权读取风险",
    "SEC002": "递归展开函数未发现终止条件，存在无限递归导致资源耗尽的风险",
    "SEC003": "敏感配置值以明文写入日志或标准输出，存在信息泄露风险",
}


@dataclasses.dataclass(frozen=True)
class Finding:
    """单条静态检查问题。

    四个字段分别对应任务要求：
    rule_id 规则标识 / file 相对路径 / line 行号 / message 一句话说明
    """

    rule_id: str
    file: str
    line: int
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id,
            "file": self.file,
            "line": self.line,
            "message": self.message,
        }
