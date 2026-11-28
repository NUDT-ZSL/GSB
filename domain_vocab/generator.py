"""Deterministic synthetic vocabulary, corpus, embeddings and labels."""

from __future__ import annotations

from dataclasses import dataclass
import random

import numpy as np

from .normalization import VERSIONS, render_identifier

# A deliberately small base vocabulary. Domain-style prefixes and several
# reusable numeric/semantic atoms are present; rare atoms are absent so the
# overlap and fallback rules can be exercised.
BASE_VOCABULARY: tuple[str, ...] = (
    "<pad>",
    "<unk>",
    "mp",
    "tar",
    "ld",
    "wx",
    "zz",
    "a",
    "b",
    "peak",
    "flat",
    "valley",
    "rain",
    "snow",
    "sun",
    "wind",
    "1042",
    "3401",
    "5501",
    "6601",
    "7701",
    "8811",
    "0000",
    "meter",
    "point",
    "tariff",
    "load",
    "weather",
    "forecast",
    "energy",
    "region",
    "demand",
)


@dataclass(frozen=True)
class DomainEntity:
    key: str
    segments: tuple[str, ...]
    target_frequency: int
    label: int
    note: str = ""

    @property
    def canonical_id(self) -> str:
        return "::".join(self.segments)


@dataclass(frozen=True)
class CorpusRow:
    line_number: int
    entity_key: str
    raw_identifiers: dict[str, str]
    label: int


@dataclass(frozen=True)
class LabeledSample:
    sample_number: int
    entity_key: str
    raw_identifier: str
    label: int
    split: str


@dataclass
class GeneratedData:
    entities: list[DomainEntity]
    base_vocab: list[str]
    base_embeddings: np.ndarray
    fixed_head: np.ndarray
    corpus: list[CorpusRow]
    labeled_samples: list[LabeledSample]


def _rng(seed: int, stream: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence([int(seed), stream]))


def _fixed_entities() -> list[DomainEntity]:
    return [
        DomainEntity("e001", ("mp", "1042", "3401", "a"), 4, 0, "计量点，高重叠"),
        DomainEntity("e002", ("mp", "2087", "3401", "b"), 2, 1, "计量点，重复子词 3401"),
        DomainEntity("e003", ("mp", "1042", "5517", "a"), 1, 0, "低频但高重叠"),
        DomainEntity("e004", ("mp", "6631", "3401", "b"), 1, 1, "低频但高重叠"),
        DomainEntity("e005", ("tar", "1042", "5501", "peak"), 3, 2, "电价套餐，高频"),
        DomainEntity("e006", ("tar", "7731", "5501", "flat"), 1, 2, "电价套餐，低频"),
        DomainEntity("e007", ("tar", "3401", "6601", "valley"), 1, 3, "电价套餐"),
        DomainEntity("e008", ("ld", "peak", "3401"), 1, 2, "负荷类型"),
        DomainEntity("e009", ("ld", "valley", "8811"), 1, 3, "负荷类型"),
        DomainEntity("e010", ("ld", "flat", "3401"), 2, 2, "负荷类型"),
        DomainEntity("e011", ("wx", "rain", "3401"), 4, 1, "气象关联，高频"),
        DomainEntity("e012", ("wx", "snow", "6601"), 1, 1, "气象关联"),
        DomainEntity("e013", ("wx", "sun", "7701"), 2, 0, "气象关联"),
        DomainEntity("e014", ("wx", "wind", "8811"), 1, 3, "气象关联"),
        DomainEntity("e015", ("zz", "9917", "4471", "q"), 1, 1, "极低频且几乎无重叠"),
        DomainEntity("e016", ("q8", "8819", "6602", "x"), 2, 3, "极低频且无重叠"),
        DomainEntity("e017", ("r3", "3401", "9973", "y"), 1, 0, "仅一个重叠子词"),
        DomainEntity("e018", ("zq", "high", "0000", "z"), 3, 2, "无共享前后缀但高频"),
        DomainEntity(
            "e019",
            ("wx", "rain-snow", "3401"),
            1,
            1,
            "v2 分隔符歧义：语义片段内部含连字符",
        ),
        DomainEntity(
            "e020",
            ("mp", "12", "345", "a"),
            1,
            0,
            "v3 数字归并歧义：非四位数原子",
        ),
        DomainEntity(
            "e021",
            ("tar", "1042", "1042", "peak"),
            1,
            2,
            "重复子词：1042 出现两次",
        ),
    ]


def _generated_entities(seed: int, count: int = 7) -> list[DomainEntity]:
    rng = _rng(seed, stream=11)
    prefixes = ("mp", "tar", "ld", "wx")
    suffixes = ("a", "b", "peak", "flat", "valley", "rain", "snow", "sun")
    labels = {"a": 0, "sun": 0, "b": 1, "rain": 1, "snow": 1, "peak": 2, "flat": 2,
              "valley": 3}
    entities: list[DomainEntity] = []
    for index in range(count):
        prefix = prefixes[index % len(prefixes)]
        suffix = suffixes[int(rng.integers(0, len(suffixes)))]
        number_a = f"{int(rng.integers(1000, 9999)):04d}"
        number_b = f"{int(rng.integers(1000, 9999)):04d}"
        frequency = int(rng.choice(np.array([1, 1, 2, 3]), p=np.array([0.45, 0.2, 0.2, 0.15])))
        key = f"e{22 + index:03d}"
        entities.append(
            DomainEntity(
                key,
                (prefix, number_a, number_b, suffix),
                frequency,
                labels[suffix],
                "按种子生成的领域标识",
            )
        )
    return entities


def _build_corpus(
    entities: list[DomainEntity],
    seed: int,
) -> list[CorpusRow]:
    rng = random.Random(int(seed) ^ 0x5EED_C0DE)
    expanded: list[DomainEntity] = []
    for entity in entities:
        expanded.extend([entity] * max(1, entity.target_frequency))
    rng.shuffle(expanded)

    rows: list[CorpusRow] = []
    for line_number, entity in enumerate(expanded, start=1):
        raw_identifiers = {
            version: render_identifier(entity.segments, version) for version in VERSIONS
        }
        rows.append(
            CorpusRow(
                line_number=line_number,
                entity_key=entity.key,
                raw_identifiers=raw_identifiers,
                label=entity.label,
            )
        )
    return rows


def _build_labeled_samples(
    entities: list[DomainEntity],
    samples: int,
    seed: int,
    primary_version: str,
) -> list[LabeledSample]:
    rng = random.Random(int(seed) ^ 0xA11C_E55E)
    pool: list[DomainEntity] = []
    repeats = max(1, (samples + len(entities) - 1) // len(entities))
    for repeat in range(repeats):
        for entity in entities:
            pool.append(entity)
    rng.shuffle(pool)
    selected = pool[:samples]

    by_entity: dict[str, list[DomainEntity]] = {}
    for entity in selected:
        by_entity.setdefault(entity.key, []).append(entity)

    train_entities: list[DomainEntity] = []
    eval_entities: list[DomainEntity] = []
    for entity_key in sorted(by_entity):
        occurrences = by_entity[entity_key]
        train_count = max(1, int(round(len(occurrences) * 0.75)))
        if train_count >= len(occurrences) and len(occurrences) > 1:
            train_count = len(occurrences) - 1
        train_entities.extend(occurrences[:train_count])
        eval_entities.extend(occurrences[train_count:])

    rng.shuffle(train_entities)
    rng.shuffle(eval_entities)
    ordered = [("train", entity) for entity in train_entities] + [
        ("eval", entity) for entity in eval_entities
    ]
    return [
        LabeledSample(
            sample_number=index,
            entity_key=entity.key,
            raw_identifier=render_identifier(entity.segments, primary_version),
            label=entity.label,
            split=split,
        )
        for index, (split, entity) in enumerate(ordered, start=1)
    ]


def generate_data(
    seed: int = 202411,
    samples: int = 168,
    embedding_dim: int = 24,
    primary_version: str = "v1",
) -> GeneratedData:
    if samples < 1:
        raise ValueError("samples must be positive")
    if embedding_dim < 4:
        raise ValueError("embedding_dim must be at least 4")

    entities = _fixed_entities() + _generated_entities(seed)
    corpus = _build_corpus(entities, seed)
    labeled_samples = _build_labeled_samples(entities, samples, seed, primary_version)

    embedding_rng = _rng(seed, stream=23)
    base_embeddings = embedding_rng.normal(
        0.0, 0.35, size=(len(BASE_VOCABULARY), embedding_dim)
    ).astype(np.float64)
    # Fixed projection head; it is never trained.
    fixed_head = embedding_rng.normal(
        0.0, 1.0 / np.sqrt(embedding_dim), size=(embedding_dim, 4)
    ).astype(np.float64)

    return GeneratedData(
        entities=entities,
        base_vocab=list(BASE_VOCABULARY),
        base_embeddings=base_embeddings,
        fixed_head=fixed_head,
        corpus=corpus,
        labeled_samples=labeled_samples,
    )

