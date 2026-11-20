"""Input, configuration and case-registry validation."""

from __future__ import annotations

import math
import threading
from typing import Iterable

from .errors import (
    E_ALIGN_INVALID,
    E_CONFIG_INVALID,
    E_DUP_CASE,
    E_TOKEN_UNKNOWN,
    fail,
)
from .models import Alignment, DecodeConfig, TranslationCase


def validate_config(config: DecodeConfig) -> None:
    if not isinstance(config.block_size, int) or config.block_size <= 0:
        fail(
            E_CONFIG_INVALID,
            "block_size must be a positive integer",
            field="block_size",
            data={"value": config.block_size},
        )
    if (
        not isinstance(config.context_window, int)
        or config.context_window <= 0
        or config.context_window > config.max_context_window
    ):
        fail(
            E_CONFIG_INVALID,
            "context_window is outside the allowed range",
            field="context_window",
            data={
                "value": config.context_window,
                "min": 1,
                "max": config.max_context_window,
            },
        )
    if (
        not math.isfinite(float(config.latency_budget_ms))
        or config.latency_budget_ms <= 0
    ):
        fail(
            E_CONFIG_INVALID,
            "latency_budget_ms must be finite and positive",
            field="latency_budget_ms",
            data={"value": config.latency_budget_ms},
        )
    for field_name in (
        "max_source_tokens",
        "max_cache_entries",
        "max_block_memory_bytes",
        "max_context_window",
        "embedding_dim",
    ):
        value = getattr(config, field_name)
        if not isinstance(value, int) or value <= 0:
            fail(
                E_CONFIG_INVALID,
                f"{field_name} must be a positive integer",
                field=field_name,
                data={"value": value},
            )
    if config.embedding_dim < 8 or config.embedding_dim > 128:
        fail(
            E_CONFIG_INVALID,
            "embedding_dim must be between 8 and 128",
            field="embedding_dim",
            data={"value": config.embedding_dim},
        )
    if config.precision < 6 or config.precision > 15:
        fail(
            E_CONFIG_INVALID,
            "precision must be between 6 and 15",
            field="precision",
            data={"value": config.precision},
        )


def validate_case(case: TranslationCase) -> None:
    if not case.case_id.strip():
        fail(
            E_CONFIG_INVALID,
            "case_id must not be empty",
            field="case_id",
        )
    if not case.source_tokens:
        fail(
            E_CONFIG_INVALID,
            "source_tokens must not be empty",
            field="source_tokens",
        )
    if len(set(case.source_vocab)) != len(case.source_vocab):
        fail(
            E_CONFIG_INVALID,
            "source_vocab contains duplicate tokens",
            field="source_vocab",
        )
    if len(set(case.target_vocab)) != len(case.target_vocab):
        fail(
            E_CONFIG_INVALID,
            "target_vocab contains duplicate tokens",
            field="target_vocab",
        )

    source_vocab = set(case.source_vocab)
    for index, token in enumerate(case.source_tokens):
        if token not in source_vocab:
            fail(
                E_TOKEN_UNKNOWN,
                "source token is not in the controlled vocabulary",
                field=f"source_tokens[{index}]",
                data={"index": index, "token": token},
            )

    target_vocab = set(case.target_vocab)
    pair_vocab = set(case.pair_vocab)
    seen_targets: dict[int, str] = {}
    for index, alignment in enumerate(case.alignments):
        if not 0 <= alignment.source_index < len(case.source_tokens):
            fail(
                E_ALIGN_INVALID,
                "alignment source_index is out of range",
                field=f"alignments[{index}].source_index",
                data={
                    "index": index,
                    "source_index": alignment.source_index,
                    "source_length": len(case.source_tokens),
                },
            )
        if alignment.target_index < 0:
            fail(
                E_ALIGN_INVALID,
                "alignment target_index must be non-negative",
                field=f"alignments[{index}].target_index",
                data={"index": index, "target_index": alignment.target_index},
            )
        if alignment.target_token not in target_vocab:
            fail(
                E_ALIGN_INVALID,
                "alignment references a target token absent from target_vocab",
                field=f"alignments[{index}].target_token",
                data={
                    "index": index,
                    "target_token": alignment.target_token,
                },
            )
        source_token = case.source_tokens[alignment.source_index]
        if (source_token, alignment.target_token) not in pair_vocab:
            fail(
                E_ALIGN_INVALID,
                "alignment pair is absent from pair_vocab",
                field=f"alignments[{index}]",
                data={
                    "index": index,
                    "source_token": source_token,
                    "target_token": alignment.target_token,
                },
            )
        if not math.isfinite(alignment.weight) or alignment.weight <= 0:
            fail(
                E_ALIGN_INVALID,
                "alignment weight must be finite and positive",
                field=f"alignments[{index}].weight",
                data={"index": index, "weight": alignment.weight},
            )
        prior = seen_targets.get(alignment.target_index)
        if prior is not None and prior != alignment.target_token:
            fail(
                E_ALIGN_INVALID,
                "one target position has conflicting target tokens",
                field=f"alignments[{index}].target_token",
                data={
                    "target_index": alignment.target_index,
                    "first_token": prior,
                    "second_token": alignment.target_token,
                },
            )
        seen_targets[alignment.target_index] = alignment.target_token

    if not case.alignments:
        fail(
            E_ALIGN_INVALID,
            "at least one alignment is required",
            field="alignments",
        )
    target_length = case.target_length
    missing = sorted(set(range(target_length)) - set(seen_targets))
    if missing:
        fail(
            E_ALIGN_INVALID,
            "target positions must form a contiguous range",
            field="alignments",
            data={"missing_target_indices": missing},
        )
    for pair in case.pair_vocab:
        if (
            len(pair) != 2
            or pair[0] not in source_vocab
            or pair[1] not in target_vocab
        ):
            fail(
                E_ALIGN_INVALID,
                "pair_vocab entry references an unknown token",
                field="pair_vocab",
                data={"entry": list(pair)},
            )


def validate_case_for_config(case: TranslationCase, config: DecodeConfig) -> None:
    validate_case(case)
    validate_config(config)
    if len(case.source_tokens) > config.max_source_tokens:
        from .errors import E_RESOURCE_LIMIT, fail as fail_resource

        fail_resource(
            E_RESOURCE_LIMIT,
            "source sentence exceeds max_source_tokens",
            field="source_tokens",
            data={
                "length": len(case.source_tokens),
                "limit": config.max_source_tokens,
            },
        )


class CaseRegistry:
    """Thread-safe duplicate-case detector."""

    def __init__(self) -> None:
        self._case_ids: set[str] = set()
        self._lock = threading.Lock()

    def add(self, case: TranslationCase) -> None:
        with self._lock:
            if case.case_id in self._case_ids:
                fail(
                    E_DUP_CASE,
                    "case_id already exists in this workbench",
                    field="case_id",
                    data={"case_id": case.case_id},
                )
            validate_case(case)
            self._case_ids.add(case.case_id)

    def contains(self, case_id: str) -> bool:
        with self._lock:
            return case_id in self._case_ids

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._case_ids))

    def remove(self, case_id: str) -> None:
        with self._lock:
            self._case_ids.discard(case_id)


def alignment_source_dependencies(
    alignments: Iterable[Alignment],
) -> dict[int, tuple[int, ...]]:
    dependencies: dict[int, set[int]] = {}
    for alignment in alignments:
        dependencies.setdefault(alignment.target_index, set()).add(
            alignment.source_index
        )
    return {
        target: tuple(sorted(sources))
        for target, sources in sorted(dependencies.items())
    }

