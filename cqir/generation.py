from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .models import Document, Paragraph, QueryTurn
from .text import term_counts
from .util import atomic_write_text, canonical_json_pretty

SEED = 20261122


DOCUMENT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "doc_id": "doc-001",
        "category": "citation",
        "paragraphs": (
            ("definition", "v1 citation 使用 doc_id、start、end 与 score 四个竖线字段，span 按 Unicode 码点计数，不包含 paragraph role 或 confidence 区间。"),
            ("mechanism", "v2 在 v1 的四字段后补充 confidence low、confidence high、paragraph role 与 order weight，从而保留排序权重和段落语义。"),
            ("caveat", "跨格式加载时必须逐项比较 citation span，任何 offset 改写或截断都会触发 span drift，不能以字符串截断掩盖错误。"),
            ("example", "当 v3 citation 带有 context chain 与 revision 时，降级到 v1 需要显式记录被丢弃的多轮上下文引用链。"),
        ),
    },
    {
        "doc_id": "doc-002",
        "category": "incremental",
        "paragraphs": (
            ("definition", "incremental invalidation 在 document version 提升后只重算受影响 recall 与 snippet，未变化文档继续复用内容寻址产物。"),
            ("mechanism", "legacy 变更词项会改变 inverted 路由得分，vector approximate 也会随文档向量变化；其他文档的逐文档阶段产物可直接复用。"),
            ("caveat", "删除文档后必须从 recall 集合移除它，并让 citation 装配与答案合成从受影响后缀重新计算。"),
            ("example", "局部 invalidation 的结果必须与使用新文档集合从零完整 recompute 的规范化字节完全一致。"),
        ),
    },
    {
        "doc_id": "doc-003",
        "category": "snapshot",
        "paragraphs": (
            ("definition", "snapshot 使用单调递增 version 和覆盖 envelope 的 checksum，引用与答案总是整体替换而不是局部修补。"),
            ("mechanism", "写入同一 cache key 时通过非阻塞独占锁控制并发，失败方立即收到 stage busy，不允许无限等待。"),
            ("caveat", "回退写入触发 snapshot rollback；内容损坏或 checksum 不符触发 snapshot corrupt，两类错误必须区分。"),
            ("example", "文档版本表、引用快照和答案快照分别具有版本号，读取时先验证 envelope 再解析业务内容。"),
        ),
    },
    {
        "doc_id": "doc-004",
        "category": "retrieval",
        "paragraphs": (
            ("definition", "retrieval 同时运行 inverted 词项召回、category popular 类别热门召回与 vector approximate 固定种子向量召回。"),
            ("mechanism", "候选按 doc_id 归并且保留每一路 route 的 raw score；最终同分时使用 doc_id 升序打破平局。"),
            ("caveat", "vector 近似使用 NumPy 固定种子特征散列，不依赖进程、GPU、系统时间或外部服务。"),
            ("example", "多路 recall 的规范化 JSON 在相同 query、文档版本和配置下必须逐字节一致。"),
        ),
    },
    {
        "doc_id": "doc-005",
        "category": "citation",
        "paragraphs": (
            ("definition", "context chain 记录多轮会话中的引用来源，revision 记录逐引用的业务修订号，二者只被 v3 结构化格式完整表达。"),
            ("mechanism", "downgrade 到 v1 时 context chain 与 revision 按显式规则丢弃，并输出每个 citation 的丢弃明细。"),
            ("caveat", "如果 v3 的 revision 不是 v1 升级策略可推导的默认值，跨格式裁决必须返回 semantic incomplete。"),
            ("example", "format 解析器依据 header 或字段集分派路径，未知版本返回 unsupported，字段损坏返回 corrupt。"),
        ),
    },
    {
        "doc_id": "doc-006",
        "category": "retrieval",
        "paragraphs": (
            ("definition", "query normalization 使用 Unicode NFKC 与 casefold，再合并同一 session 的前序轮次形成 context。"),
            ("mechanism", "上下文词项按轮次衰减合并，locale 与 session hint 保留当前轮，token 顺序始终显式排序。"),
            ("caveat", "归一化结果禁止依赖字典遍历顺序，候选集合与路名归属必须经过规范化序列化后比较。"),
            ("example", "相同 conversational query 在多轮 context merge 后产生稳定的 recall 输入与查询哈希。"),
        ),
    },
    {
        "doc_id": "doc-007",
        "category": "interop",
        "paragraphs": (
            ("definition", "format interop 读取 v1、v2、v3 三种答案与引用书写形式，并按 header 或字段集选择解析器。"),
            ("mechanism", "header 截断、竖线字段数不符、必填字段缺失、数值不可解析都属于 corrupt，并报告字段位置与行号。"),
            ("caveat", "v1 无法表达 confidence、paragraph role、context chain 或 revision 时，禁止静默默认补全后宣称语义等价。"),
            ("example", "三格式仲裁只有在 answer、snippet、citation 与引用排序完全一致时返回成功，否则列出不可还原项。"),
        ),
    },
)


QUERY_SPECS: tuple[dict[str, Any], ...] = (
    {"conv_id": "conv-001", "turn_no": 1, "query_text": "citation span 在 v1 与 v2 中如何保持 offset 一致？", "locale": "zh-CN", "session_hint": "topic=citation;channel=web"},
    {"conv_id": "conv-001", "turn_no": 2, "query_text": "v3 的 paragraph role 与 confidence 区间如何迁移？", "locale": "zh-CN", "session_hint": "topic=citation;channel=web"},
    {"conv_id": "conv-001", "turn_no": 3, "query_text": "跨格式如何检测 semantic incomplete？", "locale": "zh-CN", "session_hint": "topic=interop;channel=web"},
    {"conv_id": "conv-002", "turn_no": 1, "query_text": "incremental invalidation 如何复用 snippet 与 recall？", "locale": "zh-CN", "session_hint": "topic=incremental;channel=api"},
    {"conv_id": "conv-002", "turn_no": 2, "query_text": "document version 提升后重算哪些阶段？", "locale": "zh-CN", "session_hint": "topic=incremental;channel=api"},
    {"conv_id": "conv-002", "turn_no": 3, "query_text": "删除文档后如何保证与完整 recompute 一致？", "locale": "zh-CN", "session_hint": "topic=incremental;channel=api"},
    {"conv_id": "conv-003", "turn_no": 1, "query_text": "snapshot checksum 和 rollback 如何并发控制？", "locale": "zh-CN", "session_hint": "topic=snapshot;channel=ops"},
    {"conv_id": "conv-003", "turn_no": 2, "query_text": "cache 写入冲突如何返回 stage busy？", "locale": "zh-CN", "session_hint": "topic=snapshot;channel=ops"},
    {"conv_id": "conv-003", "turn_no": 3, "query_text": "vector approximate 与 inverted recall 如何归并？", "locale": "zh-CN", "session_hint": "topic=retrieval;channel=ops"},
)


def _build_document(spec: dict[str, Any], version: int, popularity: float) -> Document:
    paragraphs = tuple(Paragraph(role=role, text=text) for role, text in spec["paragraphs"])
    text = "\n\n".join(paragraph.text for paragraph in paragraphs)
    counts = term_counts(text)
    return Document(
        doc_id=spec["doc_id"],
        category=spec["category"],
        popularity=popularity,
        terms=counts,
        doc_len=sum(counts.values()),
        doc_version=version,
        paragraphs=paragraphs,
    )


def build_samples(seed: int = SEED) -> tuple[list[QueryTurn], dict[str, Document], dict[str, Document], dict[str, Any]]:
    rng = random.Random(seed)
    documents: dict[str, Document] = {}
    for spec in DOCUMENT_SPECS:
        popularity = round(rng.uniform(0.35, 0.95), 6)
        document = _build_document(spec, 1, popularity)
        documents[document.doc_id] = document
    queries = [QueryTurn.from_dict(spec) for spec in QUERY_SPECS]
    bumped = dict(documents)
    changed_spec = next(spec for spec in DOCUMENT_SPECS if spec["doc_id"] == "doc-002")
    changed_paragraphs = (
        ("definition", "incremental invalidation 在 document version 提升后只重算受影响 recall 与 snippet，未变化文档继续复用内容寻址产物。"),
        ("mechanism", "publish 事件会改变词项与向量得分，已经缓存的正常词项阶段仍可复用；受影响文档必须重新抽取片段。"),
        ("caveat", "删除文档后必须从 recall 集合移除它，并让 citation 装配与答案合成从受影响后缀重新计算。"),
        ("example", "局部 invalidation 的结果必须与使用新文档集合从零完整 recompute 的规范化字节完全一致。"),
    )
    changed_spec = dict(changed_spec)
    changed_spec["paragraphs"] = changed_paragraphs
    bumped["doc-002"] = _build_document(changed_spec, 2, documents["doc-002"].popularity)
    del bumped["doc-005"]
    change_set = {
        "updated": {"doc-002": {"from": 1, "to": 2, "terms_added": ["publish"], "terms_removed": ["legacy"]}},
        "deleted": ["doc-005"],
        "seed": seed,
    }
    return queries, documents, bumped, change_set


def write_samples(root: Path, seed: int = SEED) -> dict[str, Any]:
    queries, documents, bumped, change_set = build_samples(seed)
    root.mkdir(parents=True, exist_ok=True)
    query_text = "\n".join(
        json.dumps(query.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for query in queries
    ) + "\n"
    atomic_write_text(root / "queries.jsonl", query_text)
    atomic_write_text(
        root / "documents.json",
        canonical_json_pretty({doc_id: document.to_dict() for doc_id, document in sorted(documents.items())}) + "\n",
    )
    atomic_write_text(
        root / "documents_bumped.json",
        canonical_json_pretty({doc_id: document.to_dict() for doc_id, document in sorted(bumped.items())}) + "\n",
    )
    atomic_write_text(root / "change_set.json", canonical_json_pretty(change_set) + "\n")
    return {
        "root": str(root),
        "seed": seed,
        "queries": len(queries),
        "documents": len(documents),
        "bumped_documents": len(bumped),
        "updated": sorted(change_set["updated"]),
        "deleted": change_set["deleted"],
    }


def load_queries(path: Path) -> list[QueryTurn]:
    queries: list[QueryTurn] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        queries.append(QueryTurn.from_dict(json.loads(line)))
    return sorted(queries, key=lambda item: (item.conv_id, item.turn_no))


def load_documents(path: Path) -> dict[str, Document]:
    value = json.loads(path.read_text(encoding="utf-8"))
    return {
        doc_id: Document.from_dict(document)
        for doc_id, document in sorted(value.items())
    }
