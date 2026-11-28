"""End-to-end deterministic pipeline and JSON result assembly."""

from __future__ import annotations

from dataclasses import dataclass
import platform
from typing import Any

import numpy as np

from .adaptation import (
    AdapterParameters,
    evaluate_downstream,
    train_adapter,
)
from .extension import (
    ExtensionBuild,
    build_all_version_analysis,
    build_extension,
    consistency_audit,
)
from .generator import generate_data
from .normalization import VERSIONS


@dataclass(frozen=True)
class PipelineConfig:
    seed: int = 202411
    vocab_version: str = "all"
    samples: int = 168
    embedding_dim: int = 24
    epochs: int = 80
    learning_rate: float = 0.03
    cosine_threshold: float = 0.6
    uncertain_tolerance: float = 1e-3

    def __post_init__(self) -> None:
        if self.seed < 0:
            raise ValueError("seed must be non-negative")
        if self.vocab_version not in (*VERSIONS, "all"):
            raise ValueError("vocab_version must be v1, v2, v3 or all")
        if self.samples < 1:
            raise ValueError("samples must be positive")
        if self.embedding_dim < 4:
            raise ValueError("embedding_dim must be at least 4")
        if self.epochs < 1:
            raise ValueError("epochs must be positive")
        if self.learning_rate <= 0:
            raise ValueError("learning_rate must be positive")
        if not 0.0 <= self.cosine_threshold <= 1.0:
            raise ValueError("cosine_threshold must be between 0 and 1")
        if self.uncertain_tolerance < 0:
            raise ValueError("uncertain_tolerance must be non-negative")


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _adapted_new_token_rows(
    build: ExtensionBuild,
    trained: dict[str, object],
) -> list[dict[str, object]]:
    base_size = len(build.base_vocab)
    embeddings = trained["adapted_embeddings"]
    assert isinstance(embeddings, np.ndarray)
    update_stats = trained["new_token_embedding_update_stats"]
    assert isinstance(update_stats, dict)
    per_token = update_stats["per_token_update_l2_norm"]
    assert isinstance(per_token, dict)
    rows: list[dict[str, object]] = []
    for offset, candidate in enumerate(build.accepted):
        token_id = base_size + offset
        rows.append(
            {
                "new_token_id": token_id,
                "token": candidate.whole_token,
                "split_source_identifier": candidate.entity.key,
                "vector": embeddings[token_id].tolist(),
                "norm": float(np.linalg.norm(embeddings[token_id])),
                "update_l2_norm": float(per_token[candidate.whole_token]),
            }
        )
    return rows


def run_pipeline(config: PipelineConfig | None = None) -> dict[str, object]:
    config = config or PipelineConfig()
    effective_version = "v1" if config.vocab_version == "all" else config.vocab_version
    data = generate_data(
        seed=config.seed,
        samples=config.samples,
        embedding_dim=config.embedding_dim,
        primary_version=effective_version,
    )
    build = build_extension(data, effective_version)
    version_analysis = build_all_version_analysis(data)
    trained = train_adapter(
        data=data,
        build=build,
        epochs=config.epochs,
        learning_rate=config.learning_rate,
    )
    downstream = evaluate_downstream(
        data=data,
        build=build,
        trained=trained,
        uncertain_tolerance=config.uncertain_tolerance,
    )
    audit = consistency_audit(
        build=build,
        base_embeddings=data.base_embeddings,
        threshold=config.cosine_threshold,
    )

    base_size = len(data.base_vocab)
    new_tokens: list[dict[str, object]] = []
    for offset, candidate in enumerate(build.accepted):
        token_id = base_size + offset
        assert candidate.initialization is not None
        item = candidate.to_dict(token_id=token_id)
        item["initial_embedding"] = candidate.initialization.embedding.tolist()
        item["initial_embedding_norm"] = candidate.initialization.norm
        new_tokens.append(item)

    adaptation_report = {
        key: value
        for key, value in trained.items()
        if key not in {"adapted_embeddings", "adapted_adapter"}
    }
    adaptation_report["adapted_new_token_rows"] = _adapted_new_token_rows(build, trained)

    original_vocabulary = [
        {"token_id": token_id, "token": token, "kind": "original"}
        for token_id, token in enumerate(data.base_vocab)
    ]
    extended_vocabulary = original_vocabulary + [
        {
            "token_id": base_size + offset,
            "token": candidate.whole_token,
            "kind": "new",
            "source_identifier": candidate.entity.key,
        }
        for offset, candidate in enumerate(build.accepted)
    ]

    version_summary = version_analysis["summary"]
    assert isinstance(version_summary, dict)
    loss_direction_correct = float(downstream["loss_after"]) < float(
        downstream["loss_before"]
    )
    hit_not_degraded = float(downstream["hit_rate_after"]) >= float(
        downstream["hit_rate_before"]
    )
    frozen_ok = bool(adaptation_report["frozen_verification"]["original_rows_unchanged"])
    norms_finite = all(np.isfinite(item["initial_embedding_norm"]) for item in new_tokens)
    initial_norms = [float(item["initial_embedding_norm"]) for item in new_tokens]
    summary = {
        "candidate_count": len(build.candidates),
        "new_token_count": len(build.accepted),
        "fallback_count": len(build.candidates) - len(build.accepted),
        "initial_embedding_norm_min": min(initial_norms) if initial_norms else 0.0,
        "initial_embedding_norm_max": max(initial_norms) if initial_norms else 0.0,
        "loss_before": downstream["loss_before"],
        "loss_after": downstream["loss_after"],
        "loss_gain": downstream["loss_gain"],
        "hit_rate_before": downstream["hit_rate_before"],
        "hit_rate_after": downstream["hit_rate_after"],
        "original_embedding_rows_frozen": frozen_ok,
        "version_conflict_count": version_summary["conflict_entity_count"],
        "consistency_anomaly_count": len(audit["anomalies"]),
    }

    result: dict[str, object] = {
        "schema_version": "1.0",
        "summary": summary,
        "config": {
            "seed": config.seed,
            "requested_vocab_version": config.vocab_version,
            "effective_vocab_version": effective_version,
            "samples": config.samples,
            "embedding_dim": config.embedding_dim,
            "epochs": config.epochs,
            "learning_rate": config.learning_rate,
            "cosine_threshold": config.cosine_threshold,
            "uncertain_tolerance": config.uncertain_tolerance,
        },
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
        },
        "normalization_rules": {
            "v1": "下划线分隔，逐段切分",
            "v2": "连字符与斜杠都作为分隔符，逐段切分",
            "v3": "点号分隔；连续数字段先合并，再按四位数字原子回切",
            "unified_segment_joiner": "::",
        },
        "admission_rules": {
            "overlap_count_threshold": 2,
            "corpus_frequency_threshold": 3,
            "rule": "overlap_count >= 2 or corpus_frequency >= 3",
            "fallback": "reuse existing normalized subwords; <unk> when none exist",
        },
        "dataset": {
            "entity_count": len(data.entities),
            "corpus_row_count": len(data.corpus),
            "labeled_sample_count": len(data.labeled_samples),
            "train_sample_count": sum(
                1 for sample in data.labeled_samples if sample.split == "train"
            ),
            "eval_sample_count": sum(
                1 for sample in data.labeled_samples if sample.split == "eval"
            ),
            "base_vocabulary_size": base_size,
            "embedding_dim": int(data.base_embeddings.shape[1]),
            "original_vocabulary": original_vocabulary,
            "extended_vocabulary": extended_vocabulary,
            "original_embedding_matrix": data.base_embeddings.tolist(),
            "corpus": [
                {
                    "line_number": row.line_number,
                    "identifier_key": row.entity_key,
                    "identifiers": row.raw_identifiers,
                    "label": row.label,
                }
                for row in data.corpus
            ],
        },
        "version_segmentation": version_analysis["entities"],
        "version_summary": version_summary,
        "per_version_candidates": version_analysis["per_version"],
        "extension": {
            "selected_version": effective_version,
            "candidate_count": len(build.candidates),
            "accepted_count": len(build.accepted),
            "fallback_count": len(build.candidates) - len(build.accepted),
            "new_token_set": [candidate.whole_token for candidate in build.accepted],
            "candidates": [
                candidate.to_dict(
                    token_id=build.token_to_id.get(candidate.whole_token)
                    if candidate.accepted
                    else None
                )
                for candidate in build.candidates
            ],
            "new_tokens": new_tokens,
        },
        "adaptation_report": adaptation_report,
        "downstream_comparison": downstream,
        "embedding_consistency_audit": audit,
        "conflicts": version_analysis["conflicts"],
        "conflict_tokens": version_analysis["conflicts"],
        "validation": {
            "original_embedding_rows_frozen": frozen_ok,
            "initialization_norms_finite": norms_finite,
            "loss_direction_correct": loss_direction_correct,
            "hit_rate_not_degraded": hit_not_degraded,
            "cross_version_all_equivalent": bool(
                version_summary["all_versions_equivalent"]
            ),
            "cross_version_equivalent_entities": int(
                version_summary["equivalent_entity_count"]
            ),
            "cross_version_conflict_entities": int(
                version_summary["conflict_entity_count"]
            ),
            "consistency_audit_passed": bool(audit["passed"]),
        },
    }
    return _jsonable(result)

