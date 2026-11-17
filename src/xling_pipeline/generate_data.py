"""固定随机种子的本地确定性样本生成器。

生成内容：
- 多语言文档库 JSONL（含故意插入的非法记录）
- 查询集 JSONL（含故意插入的非法记录）
- 中英翻译对照表 JSONL（含一对多翻译，用于抑制逻辑验证）
- 相关性真值 JSONL（0-3 四档）

设计要点：文档与查询的“概念集合”显式生成并复用于真值标注，
避免随机数流错位导致真值与文本不一致。所有随机数均来自种子固定的
random.Random 实例，重复运行产物字节一致。
"""

import json
import os
import random

SEED = 20261117

CONCEPTS = [
    ("price", "价格"), ("quality", "质量"), ("delivery", "配送"),
    ("warranty", "保修"), ("network", "网络"), ("memory", "内存"),
    ("battery", "电池"), ("screen", "屏幕"), ("camera", "相机"),
    ("payment", "支付"), ("refund", "退款"), ("order", "订单"),
    ("invoice", "发票"), ("support", "客服"), ("software", "软件"),
    ("hardware", "硬件"), ("storage", "存储"), ("security", "安全"),
    ("discount", "折扣"), ("logistics", "物流"),
]

EXTRA_TRANSLATIONS = [
    ("price", "价钱", 0.61),
    ("screen", "荧屏", 0.58),
    ("价格", "cost", 0.61),
    ("屏幕", "display", 0.58),
]

EN_FILLERS = ["fast", "reliable", "new", "popular", "compact", "premium"]
ZH_FILLERS = ["快速", "可靠", "新款", "热门", "轻便", "高端"]
PUNCT_EN = [".", ",", ":"]
PUNCT_ZH = ["。", "，", "："]


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def build_translations():
    pairs = []
    for index, (en, zh) in enumerate(CONCEPTS):
        confidence = round(0.75 + 0.01 * (index % 20), 2)
        pairs.append({"source": en, "target": zh, "confidence": confidence})
        pairs.append({"source": zh, "target": en, "confidence": confidence})
    for source, target, confidence in EXTRA_TRANSLATIONS:
        pairs.append({"source": source, "target": target, "confidence": confidence})
    pairs.sort(key=lambda p: (p["source"], p["target"]))
    return pairs


def build_documents(rng):
    """返回 (写盘行, 有效文档概念映射)，二者共享同一概念集合。"""
    docs = []
    doc_concepts = {}
    for i in range(1, 49):
        lang = "en" if i % 2 == 1 else "zh"
        concept_ids = [rng.randrange(len(CONCEPTS)) for _ in range(3)]
        primary, secondary, tertiary = concept_ids
        doc_concepts[f"D{i:03d}"] = set(concept_ids)
        if lang == "en":
            words = [CONCEPTS[c][0] for c in concept_ids]
            fillers = [rng.choice(EN_FILLERS) for _ in range(3)]
            title = f"Doc {i:03d} {words[0]} {words[1]}"
            body_tokens = [
                words[0], words[0], words[1], words[2],
                fillers[0], words[1], fillers[1], words[0],
                fillers[2], words[2],
            ]
            rng.shuffle(body_tokens)
            body = " ".join(body_tokens) + rng.choice(PUNCT_EN)
        else:
            words = [CONCEPTS[c][1] for c in concept_ids]
            fillers = [rng.choice(ZH_FILLERS) for _ in range(3)]
            title = f"文档{i:03d}：{words[0]}{words[1]}"
            body_tokens = [
                words[0], words[0], words[1], words[2],
                fillers[0], words[1], fillers[1], words[0],
                fillers[2], words[2],
            ]
            rng.shuffle(body_tokens)
            body = "".join(body_tokens) + rng.choice(PUNCT_ZH)
        docs.append({
            "doc_id": f"D{i:03d}",
            "lang": lang,
            "title": title,
            "body": body,
        })
    docs.insert(5, {"doc_id": "D-BAD-EMPTY", "lang": "en", "title": "empty body", "body": "   "})
    docs.insert(12, {"doc_id": "D007", "lang": "en", "title": "duplicate id", "body": "duplicate price record"})
    docs.insert(20, {"doc_id": "D-BAD-LANG", "lang": "fr", "title": "bad lang", "body": "quality delivery"})
    return docs, doc_concepts


def build_queries(rng):
    """返回 (写盘行, 有效查询概念映射)。"""
    queries = []
    query_concepts = {}
    templates_en = ["{a}", "{a} {b}", "best {a}", "{a} and {b}", "reliable {a} {b}"]
    templates_zh = ["{a}", "{a}{b}", "高端{a}", "{a}与{b}", "可靠{a}{b}"]
    pinned = {1: 0, 2: 0, 5: 7, 6: 7}
    for i in range(1, 15):
        lang = "en" if i % 2 == 1 else "zh"
        a = pinned.get(i, rng.randrange(len(CONCEPTS)))
        b = rng.randrange(len(CONCEPTS))
        query_concepts[f"Q{i:03d}"] = {a, b}
        template_index = rng.randrange(len(templates_en))
        if lang == "en":
            text = templates_en[template_index].format(
                a=CONCEPTS[a][0], b=CONCEPTS[b][0]
            )
        else:
            text = templates_zh[template_index].format(
                a=CONCEPTS[a][1], b=CONCEPTS[b][1]
            )
        queries.append({"query_id": f"Q{i:03d}", "lang": lang, "text": text})
    queries.insert(3, {"query_id": "Q-BAD-EMPTY", "lang": "en", "text": ""})
    queries.insert(8, {"query_id": "Q002", "lang": "zh", "text": "重复查询编号"})
    return queries, query_concepts


def build_qrels(rng, valid_doc_ids, valid_query_ids, doc_concepts, query_concepts):
    """基于真实概念重叠构造 0-3 档真值，并补充确定性随机零档样本。"""
    qrels = []
    for query_id in valid_query_ids:
        wanted = query_concepts[query_id]
        scored = set()
        for doc_id in valid_doc_ids:
            overlap = len(wanted & doc_concepts[doc_id])
            if overlap == 0:
                continue
            if overlap >= 2:
                grade = 3
            else:
                # 命中主概念给 2 档，命中次/第三概念给 1 档
                primary = min(doc_concepts[doc_id])
                grade = 2 if primary in wanted else 1
            qrels.append({"query_id": query_id, "doc_id": doc_id, "grade": grade})
            scored.add(doc_id)
        candidates = [d for d in valid_doc_ids if d not in scored]
        for doc_id in rng.sample(candidates, 2):
            qrels.append({"query_id": query_id, "doc_id": doc_id, "grade": 0})
    qrels.sort(key=lambda row: (row["query_id"], row["doc_id"]))
    return qrels


def generate(output_dir):
    os.makedirs(output_dir, exist_ok=True)
    rng = random.Random(SEED)

    translations = build_translations()
    _write_jsonl(os.path.join(output_dir, "translations.jsonl"), translations)

    docs, doc_concepts = build_documents(rng)
    _write_jsonl(os.path.join(output_dir, "documents.jsonl"), docs)

    queries, query_concepts = build_queries(rng)
    _write_jsonl(os.path.join(output_dir, "queries.jsonl"), queries)

    valid_doc_ids = [f"D{i:03d}" for i in range(1, 49)]
    valid_query_ids = [f"Q{i:03d}" for i in range(1, 15)]
    qrel_rng = random.Random(SEED ^ 0x9E3779B9)
    qrels = build_qrels(
        qrel_rng, valid_doc_ids, valid_query_ids, doc_concepts, query_concepts
    )
    _write_jsonl(os.path.join(output_dir, "qrels.jsonl"), qrels)

    manifest = {
        "seed": SEED,
        "documents_written": len(docs),
        "queries_written": len(queries),
        "translation_pairs": len(translations),
        "qrels": len(qrels),
        "note": "含固定位置的非法记录，用于验证 E_RECORD_INVALID 隔离。",
    }
    with open(os.path.join(output_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    return manifest


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "data", "sample",
    )
    print(json.dumps(generate(target), ensure_ascii=False, indent=2))
