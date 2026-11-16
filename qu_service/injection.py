"""异常注入实验台。

注入配置（独立 JSON 文件）支持四类异常：
  - malformed.ratio        畸形查询比例（按查询 ID 哈希确定性选取，文本置为 null）
  - dict_remove            词典条目移除清单
  - rule_conflicts         规则冲突对
  - score_out_of_range     评分越界样例
每个注入项有确定性编号，命中情况逐样本记录并汇入报告。
"""
from __future__ import annotations

import json
from pathlib import Path

from .hashing import unit_interval_from_id

INJ_MALFORMED = "INJ_MALFORMED"
INJ_DICT_REMOVE = "INJ_DICT_REMOVE"
INJ_RULE_CONFLICT = "INJ_RULE_CONFLICT"
INJ_SCORE_OOR = "INJ_SCORE_OOR"


class InjectionPlan:
    def __init__(self, data: dict | None):
        data = data or {}
        self.malformed_ratio = float(data.get("malformed", {}).get("ratio", 0.0))
        self.dict_remove = list(data.get("dict_remove", []))
        self.rule_conflicts = [sorted(pair) for pair in data.get("rule_conflicts", [])]
        self.score_oor = {
            item["query_id"]: {item["intent"]: float(item["score"])}
            for item in data.get("score_out_of_range", [])
        }

    @property
    def empty(self) -> bool:
        return (self.malformed_ratio <= 0.0 and not self.dict_remove
                and not self.rule_conflicts and not self.score_oor)


def load_injection(path: Path | None) -> InjectionPlan:
    if path is None:
        return InjectionPlan(None)
    return InjectionPlan(json.loads(Path(path).read_text(encoding="utf-8")))


def select_malformed(query_ids: list[str], ratio: float) -> list[str]:
    """按查询 ID 哈希确定性地选取畸形查询，返回按字典序排列的 ID 列表。"""
    if ratio <= 0.0:
        return []
    return sorted(qid for qid in query_ids if unit_interval_from_id(qid) < ratio)


def apply_to_queries(queries: list[dict], plan: InjectionPlan) -> None:
    """就地应用查询级注入：畸形查询文本置 None，并记录命中的注入项编号。"""
    malformed_ids = set(select_malformed([q["query_id"] for q in queries], plan.malformed_ratio))
    removed = plan.dict_remove
    for query in queries:
        hits: list[str] = []
        if query["query_id"] in malformed_ids:
            query["text"] = None
            hits.append(INJ_MALFORMED)
        text = query.get("text")
        if isinstance(text, str):
            for entry in removed:
                if entry in text:
                    hits.append(f"{INJ_DICT_REMOVE}:{entry}")
        if query["query_id"] in plan.score_oor:
            for intent_name in sorted(plan.score_oor[query["query_id"]]):
                hits.append(f"{INJ_SCORE_OOR}:{query['query_id']}:{intent_name}")
        query["injections"] = hits


def apply_to_dicts(dictionaries: list[dict], plan: InjectionPlan) -> list[dict]:
    """应用词典条目移除，返回新词典结构（不修改原对象）。"""
    if not plan.dict_remove:
        return [dict(d, entries=list(d["entries"])) for d in dictionaries]
    removed = set(plan.dict_remove)
    return [
        dict(d, entries=[e for e in d["entries"] if e not in removed])
        for d in dictionaries
    ]
