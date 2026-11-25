"""输入校验与跨模块一致性自检。

输入校验在任何重计算之前执行，错误一律抛 :class:`BusinessError`，
带稳定错误码与可定位到记录的字段路径。
"""

from __future__ import annotations

from .config import ROUTE_NAMES, PipelineConfig
from .errors import BusinessError, ErrorCode


def _require_fields(record: dict, fields: tuple[str, ...], kind: str, index: int) -> None:
    for field_name in fields:
        if field_name not in record:
            raise BusinessError(
                ErrorCode.MISSING_FIELD,
                f"{kind}[{index}] 缺少字段 {field_name}",
                field_path=f"{kind}[{index}].{field_name}",
            )


def validate_documents(docs: list[dict], vocab_size: int) -> None:
    """校验文档：必填字段、doc_id 唯一、词项越界、长度合法。"""
    seen: set[str] = set()
    for index, doc in enumerate(docs):
        _require_fields(
            doc, ("doc_id", "terms", "doc_len"), "docs", index
        )
        doc_id = doc["doc_id"]
        if doc_id in seen:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                f"文档 doc_id 重复: {doc_id}",
                field_path=f"docs[{index}].doc_id",
            )
        seen.add(doc_id)
        terms = doc["terms"]
        if not isinstance(terms, list) or not terms:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "文档词项必须是非空列表",
                field_path=f"docs[{index}].terms",
            )
        for term_index, term_id in enumerate(terms):
            if not isinstance(term_id, int) or not (0 <= term_id < vocab_size):
                raise BusinessError(
                    ErrorCode.DOC_TERM_OUT_OF_RANGE,
                    f"文档 {doc_id} 的词项 {term_id} 越界，合法区间 [0, {vocab_size})",
                    field_path=f"docs[{index}].terms[{term_index}]",
                    context={"term_id": term_id, "vocab_size": vocab_size},
                )
        if not isinstance(doc["doc_len"], int) or doc["doc_len"] <= 0:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "doc_len 必须为正整数",
                field_path=f"docs[{index}].doc_len",
            )


def validate_queries(queries: list[dict], vocab_size: int) -> None:
    """校验查询：重复 query_id、词项越界、top_k 合法。"""
    seen: set[str] = set()
    for index, query in enumerate(queries):
        _require_fields(
            query, ("query_id", "terms", "top_k"), "queries", index
        )
        query_id = query["query_id"]
        if query_id in seen:
            raise BusinessError(
                ErrorCode.DUPLICATE_QUERY_ID,
                f"查询 query_id 重复: {query_id}",
                field_path=f"queries[{index}].query_id",
                context={"query_id": query_id},
            )
        seen.add(query_id)
        for term_index, term_id in enumerate(query["terms"]):
            if not isinstance(term_id, int) or not (0 <= term_id < vocab_size):
                raise BusinessError(
                    ErrorCode.DOC_TERM_OUT_OF_RANGE,
                    f"查询 {query_id} 的词项 {term_id} 越界，合法区间 [0, {vocab_size})",
                    field_path=f"queries[{index}].terms[{term_index}]",
                    context={"term": term_id, "vocab_size": vocab_size},
                )
        if not isinstance(query["top_k"], int) or query["top_k"] <= 0:
            raise BusinessError(
                ErrorCode.INVALID_CONFIG,
                "top_k 必须为正整数",
                field_path=f"queries[{index}].top_k",
            )


def validate_term_weights(term_weights: dict, vocab_size: int) -> None:
    """校验词项权重表：覆盖完整词表且权重为正。"""
    for term_id in range(vocab_size):
        key = str(term_id)
        if key not in term_weights:
            raise BusinessError(
                ErrorCode.MISSING_FIELD,
                f"词项权重表缺少词项 {key}",
                field_path=f"term_weights[{key}]",
            )
        weight = term_weights[key]
        if not isinstance(weight, (int, float)) or weight <= 0:
            raise BusinessError(
                ErrorCode.INVALID_WEIGHT,
                f"词项 {key} 权重非正: {weight}",
                field_path=f"term_weights[{key}]",
                context={"weight": weight},
            )


def validate_config(config: PipelineConfig) -> None:
    """校验四路召回配置、融合策略与资源预算。"""
    names = [route.name for route in config.routes]
    if sorted(names) != sorted(ROUTE_NAMES):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            f"四路召回名称必须恰好为 {ROUTE_NAMES}，实际为 {tuple(names)}",
            field_path="routes[].name",
        )
    if len(set(names)) != len(names):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "召回路名不允许重复",
            field_path="routes[].name",
        )
    for index, route in enumerate(config.routes):
        if route.weight <= 0:
            raise BusinessError(
                ErrorCode.INVALID_WEIGHT,
                f"召回路 {route.name} 配置权重非正: {route.weight}",
                field_path=f"routes[{index}].weight",
                context={"route": route.name, "weight": route.weight},
            )
        if route.candidate_cap <= 0:
            raise BusinessError(
                ErrorCode.INVALID_BUDGET,
                f"召回路 {route.name} 候选上限非正: {route.candidate_cap}",
                field_path=f"routes[{index}].candidate_cap",
            )
    if config.fusion.dedup_policy not in ("highest_norm", "first_hit"):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            f"未知去重口径: {config.fusion.dedup_policy}",
            field_path="fusion.dedup_policy",
        )
    if tuple(config.fusion.route_order) != tuple(
        route.name for route in config.routes
    ):
        raise BusinessError(
            ErrorCode.INVALID_CONFIG,
            "融合 route_order 必须与四路配置顺序一致且完整",
            field_path="fusion.route_order",
        )
    scheduler = config.scheduler
    for field_name, value in (
        ("workers", scheduler.workers),
        ("batch_size", scheduler.batch_size),
        ("candidate_budget", scheduler.candidate_budget),
        ("memory_budget_bytes", scheduler.memory_budget_bytes),
    ):
        if not isinstance(value, int) or value <= 0:
            raise BusinessError(
                ErrorCode.INVALID_BUDGET,
                f"调度预算 {field_name} 必须为正整数，实际为 {value}",
                field_path=f"scheduler.{field_name}",
                context={"value": value},
            )
    config.precision.validate()


def validate_inputs(
    docs: list[dict],
    queries: list[dict],
    term_weights: dict,
    config: PipelineConfig,
) -> None:
    """入口级全量校验。"""
    validate_config(config)
    validate_documents(docs, config.vocab_size)
    validate_queries(queries, config.vocab_size)
    validate_term_weights(term_weights, config.vocab_size)
