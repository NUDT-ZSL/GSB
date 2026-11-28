"""Candidate selection, token initialization and version conflict auditing."""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from .generator import DomainEntity, GeneratedData
from .normalization import (
    SEGMENT_SEPARATOR,
    VERSIONS,
    NormalizedIdentifier,
    compare_version_segmentations,
    normalize_identifier,
    render_identifier,
)

SPECIAL_TOKENS = frozenset({"<pad>", "<unk>"})


@dataclass
class InitializationResult:
    method: str
    referenced_subwords: list[dict[str, object]]
    embedding: np.ndarray

    @property
    def referenced_token_ids(self) -> list[int]:
        return [int(item["token_id"]) for item in self.referenced_subwords]

    @property
    def norm(self) -> float:
        return float(np.linalg.norm(self.embedding))

    def to_dict(self) -> dict[str, object]:
        return {
            "method": self.method,
            "referenced_subwords": self.referenced_subwords,
            "referenced_token_ids": self.referenced_token_ids,
            "norm": self.norm,
        }


@dataclass
class Candidate:
    entity: DomainEntity
    normalized: NormalizedIdentifier
    overlap_subwords: list[str]
    whole_token: str
    accepted: bool
    decision_rule: str
    fallback_tokens: list[str]
    corpus_frequency: int
    first_seen_line: int
    first_labeled_sample_number: int | None
    initialization: InitializationResult | None

    @property
    def overlap_count(self) -> int:
        return len(self.overlap_subwords)

    def to_dict(self, token_id: int | None = None) -> dict[str, object]:
        result: dict[str, object] = {
            "identifier_key": self.entity.key,
            "canonical_id": self.entity.canonical_id,
            "source_identifier": self.normalized.raw,
            "normalized_segments": list(self.normalized.segments),
            "whole_token": self.whole_token,
            "overlap_subwords": self.overlap_subwords,
            "overlap_count": self.overlap_count,
            "corpus_frequency": self.corpus_frequency,
            "first_seen_line": self.first_seen_line,
            "first_seen_corpus_line": self.first_seen_line,
            "first_seen_source": "domain_corpus",
            "first_labeled_sample_number": self.first_labeled_sample_number,
            "accepted": self.accepted,
            "decision_rule": self.decision_rule,
            "fallback_tokens": self.fallback_tokens,
            "note": self.entity.note,
        }
        if token_id is not None:
            result["new_token_id"] = token_id
        if self.initialization is not None:
            result["initialization"] = self.initialization.to_dict()
        return result


@dataclass
class ExtensionBuild:
    version: str
    base_vocab: list[str]
    candidates: list[Candidate]
    accepted: list[Candidate]
    token_to_id: dict[str, int]
    entity_to_token_id: dict[str, int]
    embeddings: np.ndarray
    fallback_tokens_by_entity: dict[str, list[str]]

    @property
    def base_vocab_size(self) -> int:
        return len(self.embeddings) - len(self.accepted)


def _first_seen_lines(data: GeneratedData) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in data.corpus:
        result.setdefault(row.entity_key, row.line_number)
    return result


def _corpus_frequencies(data: GeneratedData) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in data.corpus:
        result[row.entity_key] = result.get(row.entity_key, 0) + 1
    return result


def _first_labeled_sample_numbers(data: GeneratedData) -> dict[str, int]:
    result: dict[str, int] = {}
    for sample in data.labeled_samples:
        result.setdefault(sample.entity_key, sample.sample_number)
    return result


def _initialize_token(
    token: str,
    base_vocab: list[str],
    base_embeddings: np.ndarray,
) -> InitializationResult:
    matches: list[dict[str, object]] = []
    for token_id, existing in enumerate(base_vocab):
        if existing in SPECIAL_TOKENS or existing == token:
            continue
        if token.startswith(existing) or token.endswith(existing):
            relations = []
            if token.startswith(existing):
                relations.append("prefix")
            if token.endswith(existing):
                relations.append("suffix")
            matches.append(
                {
                    "token_id": token_id,
                    "subword": existing,
                    "relation": "+".join(relations),
                    "match_length": len(existing),
                }
            )

    if matches:
        longest = max(int(match["match_length"]) for match in matches)
        longest_matches = [match for match in matches if int(match["match_length"]) == longest]
        referenced_ids = [int(match["token_id"]) for match in longest_matches]
        embedding = base_embeddings[referenced_ids].mean(axis=0)
        return InitializationResult(
            method="longest_shared_prefix_or_suffix_mean",
            referenced_subwords=longest_matches,
            embedding=embedding.astype(np.float64, copy=True),
        )

    embedding = base_embeddings.mean(axis=0)
    return InitializationResult(
        method="original_embedding_matrix_global_mean",
        referenced_subwords=[],
        embedding=embedding.astype(np.float64, copy=True),
    )


def _make_candidate(
    *,
    entity: DomainEntity,
    normalized: NormalizedIdentifier,
    base_vocab: list[str],
    base_embeddings: np.ndarray,
    corpus_frequency: int,
    first_seen_line: int,
    first_labeled_sample_number: int | None,
) -> Candidate:
    base_token_set = set(base_vocab) - SPECIAL_TOKENS
    overlap = sorted({segment for segment in normalized.segments if segment in base_token_set})
    whole_token = normalized.token
    accepted = len(overlap) >= 2 or corpus_frequency >= 3
    if len(overlap) >= 2 and corpus_frequency >= 3:
        rule = "overlap_count>=2 and corpus_frequency>=3"
    elif len(overlap) >= 2:
        rule = "overlap_count>=2"
    elif corpus_frequency >= 3:
        rule = "corpus_frequency>=3"
    else:
        rule = "fallback_to_existing_subwords"

    fallback_tokens = [segment for segment in normalized.segments if segment in base_token_set]
    if not fallback_tokens:
        fallback_tokens = ["<unk>"]

    initialization = (
        _initialize_token(whole_token, base_vocab, base_embeddings) if accepted else None
    )
    return Candidate(
        entity=entity,
        normalized=normalized,
        overlap_subwords=overlap,
        whole_token=whole_token,
        accepted=accepted,
        decision_rule=rule,
        fallback_tokens=fallback_tokens,
        corpus_frequency=corpus_frequency,
        first_seen_line=first_seen_line,
        first_labeled_sample_number=first_labeled_sample_number,
        initialization=initialization,
    )


def build_extension(data: GeneratedData, version: str) -> ExtensionBuild:
    if version not in VERSIONS:
        raise ValueError(f"unsupported vocabulary version: {version}")
    frequencies = _corpus_frequencies(data)
    first_seen = _first_seen_lines(data)
    first_labeled = _first_labeled_sample_numbers(data)
    candidates: list[Candidate] = []
    for entity in data.entities:
        normalized = normalize_identifier(render_identifier(entity.segments, version), version)
        candidates.append(
            _make_candidate(
                entity=entity,
                normalized=normalized,
                base_vocab=data.base_vocab,
                base_embeddings=data.base_embeddings,
                corpus_frequency=frequencies.get(entity.key, 0),
                first_seen_line=first_seen.get(entity.key, 0),
                first_labeled_sample_number=first_labeled.get(entity.key),
            )
        )

    accepted = [candidate for candidate in candidates if candidate.accepted]
    embeddings = np.vstack(
        [data.base_embeddings]
        + [candidate.initialization.embedding[None, :] for candidate in accepted]
    )
    token_to_id: dict[str, int] = {}
    entity_to_token_id: dict[str, int] = {}
    base_size = len(data.base_vocab)
    for offset, candidate in enumerate(accepted):
        token_to_id[candidate.whole_token] = base_size + offset
        entity_to_token_id[candidate.entity.key] = base_size + offset
    fallback_map = {candidate.entity.key: candidate.fallback_tokens for candidate in candidates}
    return ExtensionBuild(
        version=version,
        base_vocab=list(data.base_vocab),
        candidates=candidates,
        accepted=accepted,
        token_to_id=token_to_id,
        entity_to_token_id=entity_to_token_id,
        embeddings=embeddings,
        fallback_tokens_by_entity=fallback_map,
    )


def build_all_version_analysis(data: GeneratedData) -> dict[str, object]:
    entity_entries: list[dict[str, object]] = []
    version_candidates: dict[str, dict[str, Candidate]] = {version: {} for version in VERSIONS}
    frequencies = _corpus_frequencies(data)
    first_seen = _first_seen_lines(data)
    first_labeled = _first_labeled_sample_numbers(data)

    for entity in data.entities:
        segmentations: dict[str, NormalizedIdentifier] = {}
        version_details: dict[str, object] = {}
        for version in VERSIONS:
            normalized = normalize_identifier(render_identifier(entity.segments, version), version)
            segmentations[version] = normalized
            candidate = _make_candidate(
                entity=entity,
                normalized=normalized,
                base_vocab=data.base_vocab,
                base_embeddings=data.base_embeddings,
                corpus_frequency=frequencies.get(entity.key, 0),
                first_seen_line=first_seen.get(entity.key, 0),
                first_labeled_sample_number=first_labeled.get(entity.key),
            )
            version_candidates[version][entity.key] = candidate
            version_details[version] = {
                "raw": normalized.raw,
                "segments": list(normalized.segments),
                "normalized_token": normalized.token,
                "overlap_subwords": candidate.overlap_subwords,
                "overlap_count": candidate.overlap_count,
                "corpus_frequency": candidate.corpus_frequency,
                "first_seen_corpus_line": candidate.first_seen_line,
                "first_labeled_sample_number": candidate.first_labeled_sample_number,
                "accepted": candidate.accepted,
                "decision_rule": candidate.decision_rule,
                "initialization": (
                    candidate.initialization.to_dict() if candidate.initialization else None
                ),
                "initial_embedding": (
                    candidate.initialization.embedding.tolist()
                    if candidate.initialization
                    else None
                ),
                "initial_embedding_norm": (
                    candidate.initialization.norm if candidate.initialization else None
                ),
            }

        comparison = compare_version_segmentations(segmentations)
        entity_entries.append(
            {
                "identifier_key": entity.key,
                "canonical_id": entity.canonical_id,
                "canonical_segments": list(entity.segments),
                "versions": version_details,
                "differences": comparison,
            }
        )

    conflict_entries: list[dict[str, object]] = []
    equivalent_count = 0
    for entry in entity_entries:
        entity_key = str(entry["identifier_key"])
        versions = entry["versions"]
        assert isinstance(versions, dict)
        tokens = {
            version: versions[version]["normalized_token"]  # type: ignore[index]
            for version in VERSIONS
        }
        accepted = {
            version: bool(versions[version]["accepted"])  # type: ignore[index]
            for version in VERSIONS
        }
        init_embeddings: dict[str, np.ndarray | None] = {}
        for version in VERSIONS:
            candidate = version_candidates[version][entity_key]
            init_embeddings[version] = (
                candidate.initialization.embedding if candidate.initialization else None
            )

        token_mismatch = len(set(tokens.values())) > 1
        acceptance_mismatch = len(set(accepted.values())) > 1
        init_mismatch = False
        available = [value for value in init_embeddings.values() if value is not None]
        if len(available) > 1:
            first = available[0]
            init_mismatch = any(
                not np.array_equal(first, value) for value in available[1:]
            )
        differences = entry["differences"]
        assert isinstance(differences, dict)
        has_segmentation_difference = bool(differences["token_set_difference"])

        if token_mismatch or acceptance_mismatch or init_mismatch or has_segmentation_difference:
            reasons = list(differences.get("reasons", []))  # type: ignore[arg-type]
            if acceptance_mismatch and "admission_rule_mismatch" not in reasons:
                reasons.append("admission_rule_mismatch")
            if init_mismatch and "initialization_mismatch" not in reasons:
                reasons.append("initialization_mismatch")
            if not reasons:
                reasons.append("version_token_mismatch")

            review: list[str] = []
            if "delimiter_ambiguity" in reasons:
                review.append(
                    "v2 同时把 '-' 和 '/' 当作分隔符，语义片段内部若含这些字符会产生额外切分；"
                    "建议确认原始字段边界。"
                )
            if "numeric_merge_ambiguity" in reasons:
                review.append(
                    "v3 合并连续数字段后按四位原子回切，非四位数字段会出现切分冲突；"
                    "建议确认数字段宽度或提供边界元数据。"
                )
            if "admission_rule_mismatch" in reasons:
                review.append("各版本重叠子词数不同，导致新增词元准入结果不一致。")
            if not review:
                review.append("三版本归一化结果不完全一致，建议人工核对字段定义。")

            version_initializations = {
                version: versions[version]["initialization"]  # type: ignore[index]
                for version in VERSIONS
            }
            conflict_entries.append(
                {
                    "identifier_key": entity_key,
                    "canonical_id": entry["canonical_id"],
                    "canonical_segments": entry["canonical_segments"],
                    "reasons": reasons,
                    "version_tokens": tokens,
                    "version_segments": differences["version_segments"],
                    "version_accepted": accepted,
                    "version_initializations": version_initializations,
                    "version_details": versions,
                    "suggested_review": review,
                }
            )
        else:
            equivalent_count += 1

    per_version_summary: dict[str, object] = {}
    for version in VERSIONS:
        accepted_candidates = [
            candidate
            for candidate in version_candidates[version].values()
            if candidate.accepted
        ]
        per_version_summary[version] = {
            "accepted_token_count": len(accepted_candidates),
            "new_tokens": [candidate.whole_token for candidate in accepted_candidates],
            "fallback_identifier_keys": [
                candidate.entity.key
                for candidate in version_candidates[version].values()
                if not candidate.accepted
            ],
        }

    return {
        "entities": entity_entries,
        "conflicts": conflict_entries,
        "summary": {
            "entity_count": len(data.entities),
            "equivalent_entity_count": equivalent_count,
            "conflict_entity_count": len(conflict_entries),
            "all_versions_equivalent": len(conflict_entries) == 0,
        },
        "per_version": per_version_summary,
    }


def cosine_similarity(left: np.ndarray, right: np.ndarray) -> float:
    denominator = float(np.linalg.norm(left) * np.linalg.norm(right))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(left, right) / denominator)


def consistency_audit(
    build: ExtensionBuild,
    base_embeddings: np.ndarray,
    threshold: float = 0.6,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    anomalies: list[dict[str, object]] = []
    base_count = int(base_embeddings.shape[0])

    for offset, candidate in enumerate(build.accepted):
        assert candidate.initialization is not None
        token_id = base_count + offset
        embedding = build.embeddings[token_id]
        basis = candidate.initialization.referenced_token_ids
        if basis:
            comparisons = [
                {
                    "token_id": basis_id,
                    "subword": str(
                        candidate.initialization.referenced_subwords[index]["subword"]
                    ),
                    "cosine_similarity": cosine_similarity(embedding, base_embeddings[basis_id]),
                }
                for index, basis_id in enumerate(basis)
            ]
            comparison_scope = "shared_prefix_or_suffix_subwords"
        else:
            comparisons = [
                {
                    "token_id": existing_id,
                    "subword": build.base_vocab[existing_id],
                    "cosine_similarity": cosine_similarity(
                        embedding, base_embeddings[existing_id]
                    ),
                }
                for existing_id in range(base_count)
            ]
            comparison_scope = "all_original_subwords"

        if comparisons:
            valid_cosines = [float(item["cosine_similarity"]) for item in comparisons]
            minimum = min(valid_cosines)
            maximum = max(valid_cosines)
        else:
            minimum = 0.0
            maximum = 0.0

        is_anomaly = minimum < threshold
        entry = {
            "new_token_id": token_id,
            "new_token": candidate.whole_token,
            "split_source_identifier": candidate.entity.key,
            "source_identifier": candidate.normalized.raw,
            "initialization_method": candidate.initialization.method,
            "initialization_basis": candidate.initialization.to_dict(),
            "comparison_scope": comparison_scope,
            "minimum_cosine_similarity": minimum,
            "maximum_cosine_similarity": maximum,
            "threshold": threshold,
            "passed": not is_anomaly,
            "comparisons": comparisons,
        }
        entries.append(entry)
        if is_anomaly:
            anomalies.append(
                {
                    "new_token_id": token_id,
                    "new_token": candidate.whole_token,
                    "split_source_identifier": candidate.entity.key,
                    "source_identifier": candidate.normalized.raw,
                    "initialization_basis": candidate.initialization.to_dict(),
                    "minimum_cosine_similarity": minimum,
                    "threshold": threshold,
                    "review_reason": (
                        "无共享前后缀，全局均值初始化与原词表最邻近向量余弦低于阈值"
                        if not basis
                        else "参考子词均值初始化与至少一个共享子词的余弦低于阈值"
                    ),
                }
            )

    return {
        "threshold": threshold,
        "checked_token_count": len(entries),
        "passed": len(anomalies) == 0,
        "anomalies": anomalies,
        "tokens": entries,
    }

