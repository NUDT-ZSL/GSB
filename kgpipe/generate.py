from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DEFAULT_CONFIG
from .util import (
    atomic_write_json,
    atomic_write_text,
    attach_checksum,
    canonical_json,
)


@dataclass(frozen=True)
class EntitySpec:
    entity_id: str
    canonical_name: str
    entity_type: str
    type_id: int
    aliases: tuple[str, ...]


ENTITIES: tuple[EntitySpec, ...] = (
    EntitySpec("PER_ZHANG", "张三", "PERSON", 10, ("老张", "张先生")),
    EntitySpec("PER_LI", "李四", "PERSON", 10, ("小李",)),
    EntitySpec("PER_WANG", "王五", "PERSON", 10, ("老王",)),
    EntitySpec("PER_ZHAO", "赵六", "PERSON", 10, ()),
    EntitySpec("PER_QIAN", "钱七", "PERSON", 10, ()),
    EntitySpec("PER_SUN", "孙八", "PERSON", 10, ()),
    EntitySpec("PER_ZHOU", "周九", "PERSON", 10, ()),
    EntitySpec("PER_WU", "吴十", "PERSON", 10, ()),
    EntitySpec("PER_ZHENG", "郑一", "PERSON", 10, ()),
    EntitySpec("PER_FENG", "冯二", "PERSON", 10, ()),
    EntitySpec("PER_CHEN", "陈三", "PERSON", 10, ()),
    EntitySpec("PER_CHU", "褚四", "PERSON", 10, ()),
    EntitySpec(
        "ORG_NUDT",
        "国防科技大学",
        "ORGANIZATION",
        20,
        ("国防科大", "NUDT"),
    ),
    EntitySpec("ORG_XINGHAI", "星海科技", "ORGANIZATION", 20, ("星海公司",)),
    EntitySpec("ORG_YUNFAN", "云帆研究院", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_QIMING", "启明实验室", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_BEICHEN", "北辰集团", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_HAICHUAN", "海川银行", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_YUANSHAN", "远山医院", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_QINGHE", "青禾学校", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_NANLING", "南岭公司", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_DONGJIANG", "东江工厂", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_XICHENG", "西城中心", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_BEIGU", "北固机构", "ORGANIZATION", 20, ()),
    EntitySpec("ORG_ZHONGXING", "中兴研究所", "ORGANIZATION", 20, ()),
    EntitySpec("LOC_CHANGSHA", "长沙", "LOCATION", 30, ("长沙市",)),
    EntitySpec("LOC_BEIJING", "北京", "LOCATION", 30, ("北京市",)),
    EntitySpec("LOC_SHANGHAI", "上海", "LOCATION", 30, ()),
    EntitySpec("LOC_SHENZHEN", "深圳", "LOCATION", 30, ()),
)


PATTERN_ROWS: tuple[dict[str, Any], ...] = (
    {
        "line_no": 1,
        "pattern_id": "P_EMPLOYED_BY",
        "relation_type": "EMPLOYED_BY",
        "relation_type_id": 100,
        "head_type": "PERSON",
        "tail_type": "ORGANIZATION",
        "trigger": "任职于",
        "max_gap": 64,
        "mandatory": True,
        "score_components": {
            "type_bp": 3000,
            "trigger_bp": 5000,
            "distance_bonus_bp": 2000,
            "gap_penalty_bp": 100,
        },
    },
    {
        "line_no": 2,
        "pattern_id": "P_BORN_IN",
        "relation_type": "BORN_IN",
        "relation_type_id": 200,
        "head_type": "PERSON",
        "tail_type": "LOCATION",
        "trigger": "出生于",
        "max_gap": 64,
        "mandatory": False,
        "score_components": {
            "type_bp": 3000,
            "trigger_bp": 5000,
            "distance_bonus_bp": 2000,
            "gap_penalty_bp": 100,
        },
    },
    {
        "line_no": 3,
        "pattern_id": "P_LOCATED_IN",
        "relation_type": "LOCATED_IN",
        "relation_type_id": 300,
        "head_type": "ORGANIZATION",
        "tail_type": "LOCATION",
        "trigger": "位于",
        "max_gap": 200,
        "mandatory": False,
        "score_components": {
            "type_bp": 3000,
            "trigger_bp": 4500,
            "distance_bonus_bp": 2000,
            "gap_penalty_bp": 80,
        },
    },
    {
        "line_no": 4,
        "pattern_id": "P_FOUNDED",
        "relation_type": "FOUNDED",
        "relation_type_id": 400,
        "head_type": "PERSON",
        "tail_type": "ORGANIZATION",
        "trigger": "创立",
        "max_gap": 64,
        "mandatory": False,
        "score_components": {
            "type_bp": 3000,
            "trigger_bp": 4500,
            "distance_bonus_bp": 2000,
            "gap_penalty_bp": 100,
        },
    },
)


@dataclass(frozen=True)
class SentenceSpec:
    text: str
    relations: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class DocumentSpec:
    doc_id: str
    shard_id: str
    sentences: tuple[SentenceSpec, ...]


DOCUMENTS: tuple[DocumentSpec, ...] = (
    DocumentSpec(
        "doc-0001",
        "shard-0001",
        (
            SentenceSpec("张三任职于国防科技大学。", (("张三", "国防科技大学", "EMPLOYED_BY"),)),
            SentenceSpec("李四出生于长沙市。", (("李四", "长沙市", "BORN_IN"),)),
            SentenceSpec("国防科技大学位于长沙。", (("国防科技大学", "长沙", "LOCATED_IN"),)),
            SentenceSpec("王五任职于星海科技。", (("王五", "星海科技", "EMPLOYED_BY"),)),
        ),
    ),
    DocumentSpec(
        "doc-0002",
        "shard-0001",
        (
            SentenceSpec("张三出生于长沙。", (("张三", "长沙", "BORN_IN"),)),
            SentenceSpec(
                "云帆研究院、启明实验室、北辰集团、海川银行、远山医院、"
                "青禾学校、南岭公司、东江工厂、西城中心、北固机构、"
                "中兴研究所均位于北京。",
                (
                    ("云帆研究院", "北京", "LOCATED_IN"),
                    ("启明实验室", "北京", "LOCATED_IN"),
                    ("北辰集团", "北京", "LOCATED_IN"),
                    ("海川银行", "北京", "LOCATED_IN"),
                    ("远山医院", "北京", "LOCATED_IN"),
                    ("青禾学校", "北京", "LOCATED_IN"),
                    ("南岭公司", "北京", "LOCATED_IN"),
                    ("东江工厂", "北京", "LOCATED_IN"),
                    ("西城中心", "北京", "LOCATED_IN"),
                    ("北固机构", "北京", "LOCATED_IN"),
                    ("中兴研究所", "北京", "LOCATED_IN"),
                ),
            ),
        ),
    ),
    DocumentSpec(
        "doc-0003",
        "shard-0002",
        (
            SentenceSpec("老张任职于国防科大。", (("老张", "国防科大", "EMPLOYED_BY"),)),
            SentenceSpec("张先生出生于北京市。", (("张先生", "北京市", "BORN_IN"),)),
            SentenceSpec("小李任职于星海公司。", (("小李", "星海公司", "EMPLOYED_BY"),)),
        ),
    ),
    DocumentSpec(
        "doc-0004",
        "shard-0002",
        (
            SentenceSpec("今日长沙天气晴。"),
            SentenceSpec("国防科技大学位于长沙。", (("国防科技大学", "长沙", "LOCATED_IN"),)),
            SentenceSpec("赵六任职于云帆研究院。", (("赵六", "云帆研究院", "EMPLOYED_BY"),)),
        ),
    ),
    DocumentSpec(
        "doc-0005",
        "shard-0003",
        (
            SentenceSpec("钱七任职于海川银行。", (("钱七", "海川银行", "EMPLOYED_BY"),)),
            SentenceSpec("孙八出生于上海。", (("孙八", "上海", "BORN_IN"),)),
        ),
    ),
    DocumentSpec(
        "doc-0006",
        "shard-0003",
        (
            SentenceSpec("周九任职于远山医院。", (("周九", "远山医院", "EMPLOYED_BY"),)),
            SentenceSpec("周九出生于深圳。", (("周九", "深圳", "BORN_IN"),)),
        ),
    ),
    DocumentSpec(
        "doc-0007",
        "shard-0004",
        (
            SentenceSpec("吴十任职于青禾学校。", (("吴十", "青禾学校", "EMPLOYED_BY"),)),
            SentenceSpec("郑一任职于南岭公司。", (("郑一", "南岭公司", "EMPLOYED_BY"),)),
        ),
    ),
    DocumentSpec(
        "doc-0008",
        "shard-0004",
        (
            SentenceSpec("冯二任职于东江工厂。", (("冯二", "东江工厂", "EMPLOYED_BY"),)),
            SentenceSpec("陈三任职于西城中心。", (("陈三", "西城中心", "EMPLOYED_BY"),)),
        ),
    ),
    DocumentSpec(
        "doc-0009",
        "shard-0004",
        (
            SentenceSpec("褚四任职于北固机构。", (("褚四", "北固机构", "EMPLOYED_BY"),)),
            SentenceSpec("褚四出生于北京。", (("褚四", "北京", "BORN_IN"),)),
        ),
    ),
    DocumentSpec(
        "doc-0010",
        "shard-0004",
        (
            SentenceSpec("王五任职于中兴研究所。", (("王五", "中兴研究所", "EMPLOYED_BY"),)),
            SentenceSpec("中兴研究所位于深圳。", (("中兴研究所", "深圳", "LOCATED_IN"),)),
        ),
    ),
)


def _dictionary_payload() -> dict[str, Any]:
    return attach_checksum(
        {
            "dict_version": 1,
            "name": "kgpipe-deterministic-zh-dictionary",
            "normalization_map": {},
            "entries": [
                {
                    "entity_id": entity.entity_id,
                    "canonical_name": entity.canonical_name,
                    "type": entity.entity_type,
                    "type_id": entity.type_id,
                    "aliases": list(entity.aliases),
                }
                for entity in ENTITIES
            ],
        }
    )


def _schema_payload() -> dict[str, Any]:
    return attach_checksum(
        {
            "schema_version": 1,
            "name": "kgpipe-deterministic-relation-schema",
            "patterns": [dict(row) for row in PATTERN_ROWS],
        }
    )


def _surface_to_entity() -> dict[str, EntitySpec]:
    result: dict[str, EntitySpec] = {}
    for entity in ENTITIES:
        for surface in (entity.canonical_name, *entity.aliases):
            result[surface] = entity
    return result


def _truth_for_documents() -> dict[str, Any]:
    surface_map = _surface_to_entity()
    triples: list[dict[str, Any]] = []
    for document in DOCUMENTS:
        sentence_offsets: dict[str, int] = {}
        for sent_no, sentence in enumerate(document.sentences):
            for head_surface, tail_surface, relation_type in sentence.relations:
                head_start = sentence.text.find(head_surface)
                tail_start = sentence.text.find(tail_surface)
                if head_start < 0 or tail_start < 0:
                    raise RuntimeError(
                        f"样本模板字面量不存在: {document.doc_id} {sentence.text}"
                    )
                if head_start >= tail_start:
                    raise RuntimeError(
                        f"样本模板头尾顺序错误: {document.doc_id} {sentence.text}"
                    )
                head = surface_map[head_surface]
                tail = surface_map[tail_surface]
                triples.append(
                    {
                        "doc_id": document.doc_id,
                        "sent_no": sent_no,
                        "head_entity_id": head.entity_id,
                        "tail_entity_id": tail.entity_id,
                        "relation_type": relation_type,
                        "head_surface": head_surface,
                        "tail_surface": tail_surface,
                        "head_span": {
                            "start": head_start,
                            "end": head_start + len(head_surface),
                        },
                        "tail_span": {
                            "start": tail_start,
                            "end": tail_start + len(tail_surface),
                        },
                    }
                )
    return attach_checksum(
        {
            "truth_version": 1,
            "name": "kgpipe-deterministic-truth",
            "triples": triples,
        }
    )


def generate_sample_data(
    output_dir: str | Path,
    *,
    seed: int = 20261121,
) -> dict[str, Any]:
    destination = Path(output_dir)
    shard_rotation = seed % 4
    shards_dir = destination / "shards"
    shards_dir.mkdir(parents=True, exist_ok=True)
    for old_file in shards_dir.glob("*.jsonl"):
        old_file.unlink()

    dictionary = _dictionary_payload()
    schema = _schema_payload()
    truth = _truth_for_documents()
    config = attach_checksum(dict(DEFAULT_CONFIG))
    atomic_write_json(destination / "dictionary.json", dictionary)
    atomic_write_json(destination / "relation_schema.json", schema)
    atomic_write_json(destination / "truth.json", truth)
    atomic_write_json(destination / "config.json", config)

    by_shard: dict[str, list[dict[str, Any]]] = {}
    for document in DOCUMENTS:
        base_shard_index = int(document.shard_id.rsplit("-", 1)[1]) - 1
        shard_id = f"shard-{((base_shard_index + shard_rotation) % 4) + 1:04d}"
        for sent_no, sentence in enumerate(document.sentences):
            by_shard.setdefault(shard_id, []).append(
                {
                    "shard_id": shard_id,
                    "doc_id": document.doc_id,
                    "sent_no": sent_no,
                    "text": sentence.text,
                    "char_len": len(sentence.text),
                }
            )
    for shard_id in sorted(by_shard):
        rows = sorted(
            by_shard[shard_id],
            key=lambda row: (row["doc_id"], row["sent_no"]),
        )
        content = "".join(canonical_json(row) + "\n" for row in rows)
        atomic_write_text(shards_dir / f"{shard_id}.jsonl", content)

    metadata = {
        "generator_version": 1,
        "seed": seed,
        "shard_rotation": shard_rotation,
        "shard_count": len(by_shard),
        "document_count": len(DOCUMENTS),
        "sentence_count": sum(len(document.sentences) for document in DOCUMENTS),
        "truth_triple_count": len(truth["triples"]),
        "dictionary_version": dictionary["dict_version"],
        "dictionary_checksum": dictionary["checksum"],
        "relation_schema_version": schema["schema_version"],
        "relation_schema_checksum": schema["checksum"],
        "truth_version": truth["truth_version"],
        "truth_checksum": truth["checksum"],
        "config_version": config["config_version"],
        "config_checksum": config["checksum"],
    }
    atomic_write_json(destination / "metadata.json", metadata)
    return metadata

