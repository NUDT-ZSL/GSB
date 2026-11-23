"""Input, configuration, and resource validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .errors import BusinessError
from .models import (
    STAGES,
    WEIGHT_NAMES,
    Session,
    SummaryConfig,
    Vocabulary,
    config_from_dict,
    session_from_dict,
    vocabulary_from_dict,
)
from .tokenization import tokenize_text


PRECISIONS = ("fp16", "fp32", "fp64")


def load_vocabulary(path: Path) -> Vocabulary:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "E_INPUT_INVALID",
            f"cannot read vocabulary {path}: {exc}",
            field="vocabulary",
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            "E_INPUT_INVALID", "vocabulary must be a JSON object", field="vocabulary"
        )
    try:
        vocab = vocabulary_from_dict(value)
    except (KeyError, TypeError, ValueError) as exc:
        raise BusinessError(
            "E_INPUT_INVALID", f"invalid vocabulary: {exc}", field="vocabulary"
        ) from exc
    if not vocab.vocab_version:
        raise BusinessError(
            "E_INPUT_INVALID", "vocab_version must not be empty", field="vocab_version"
        )
    if not vocab.words:
        raise BusinessError(
            "E_INPUT_INVALID", "vocabulary words must not be empty", field="words"
        )
    duplicates = sorted(
        {word for word in vocab.words if vocab.words.count(word) > 1}
    )
    if duplicates:
        raise BusinessError(
            "E_INPUT_INVALID",
            "duplicate vocabulary words",
            field="words",
            details={"duplicates": duplicates},
        )
    return vocab


def load_config(path: Path) -> SummaryConfig:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise BusinessError(
            "E_INPUT_INVALID", f"cannot read config {path}: {exc}", field="config"
        ) from exc
    if not isinstance(value, dict):
        raise BusinessError(
            "E_INPUT_INVALID", "config must be a JSON object", field="config"
        )
    try:
        config = config_from_dict(value)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "E_INPUT_INVALID", f"invalid config: {exc}", field="config"
        ) from exc
    validate_config(config)
    return config


def load_sessions(path: Path) -> List[Session]:
    sessions: List[Session] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    sessions.append(session_from_dict(value))
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    raise BusinessError(
                        "E_INPUT_INVALID",
                        f"invalid session at line {line_number}: {exc}",
                        field=f"sessions[{line_number}]",
                    ) from exc
    except OSError as exc:
        raise BusinessError(
            "E_INPUT_INVALID", f"cannot read sessions {path}: {exc}", field="sessions"
        ) from exc
    validate_sessions(sessions)
    return sessions


def validate_config(config: SummaryConfig) -> None:
    if config.precision not in PRECISIONS:
        raise BusinessError(
            "E_PRECISION_INVALID",
            f"unsupported precision {config.precision!r}",
            field="precision",
            details={"allowed": list(PRECISIONS)},
        )
    if config.block_size <= 0:
        raise BusinessError(
            "E_BLOCK_SIZE_INVALID",
            "block_size must be positive",
            field="block_size",
            details={"value": config.block_size},
        )
    if not (0.0 <= config.rewrite_strength <= 1.0):
        raise BusinessError(
            "E_STRENGTH_INVALID",
            "rewrite_strength must be within [0, 1]",
            field="rewrite_strength",
            details={"value": config.rewrite_strength},
        )
    if config.smoothing < 0.0:
        raise BusinessError(
            "E_SMOOTHING_INVALID",
            "smoothing must be non-negative",
            field="smoothing",
            details={"value": config.smoothing},
        )
    for name in WEIGHT_NAMES:
        if name not in config.weights:
            raise BusinessError(
                "E_WEIGHT_INVALID",
                f"weight {name!r} is missing",
                field=f"weights.{name}",
            )
        weight = float(config.weights[name])
        if weight <= 0.0:
            raise BusinessError(
                "E_WEIGHT_INVALID",
                f"weight {name!r} must be positive",
                field=f"weights.{name}",
                details={"weight": name, "value": weight},
            )
    if config.resource_policy not in ("error", "degrade"):
        raise BusinessError(
            "E_RESOURCE_POLICY_INVALID",
            "resource_policy must be 'error' or 'degrade'",
            field="resource_policy",
        )
    for field, value in (
        ("max_sessions_per_stage", config.max_sessions_per_stage),
        ("max_resident_frequency", config.max_resident_frequency),
        ("max_intermediate_memory_bytes", config.max_intermediate_memory_bytes),
    ):
        if value <= 0:
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                f"{field} must be positive",
                field=field,
                details={"limit": field, "value": value},
            )


def validate_sessions(sessions: Sequence[Session]) -> None:
    seen_ids: set[str] = set()
    for session in sessions:
        if session.session_id in seen_ids:
            raise BusinessError(
                "E_DUPLICATE_SESSION",
                f"duplicate session_id {session.session_id!r}",
                session_id=session.session_id,
                field="session_id",
            )
        seen_ids.add(session.session_id)
        validate_session_shape(session)


def validate_session_shape(session: Session) -> None:
    if not session.turns:
        raise BusinessError(
            "E_INPUT_INVALID",
            "session must contain at least one turn",
            session_id=session.session_id,
            field="turns",
        )
    turn_indices = [turn.turn_index for turn in session.turns]
    if len(turn_indices) != len(set(turn_indices)):
        raise BusinessError(
            "E_INPUT_INVALID",
            "turn_index values must be unique",
            session_id=session.session_id,
            field="turns.turn_index",
        )
    if sorted(turn_indices) != list(range(min(turn_indices), max(turn_indices) + 1)):
        raise BusinessError(
            "E_INPUT_INVALID",
            "turn_index values must be contiguous",
            session_id=session.session_id,
            field="turns.turn_index",
        )
    known_turns = set(turn_indices)
    unit_ids: set[str] = set()
    unit_keys: set[Tuple[int, int]] = set()
    for unit in session.units:
        if unit.unit_id in unit_ids:
            raise BusinessError(
                "E_INPUT_INVALID",
                f"duplicate unit_id {unit.unit_id!r}",
                session_id=session.session_id,
                field="units.unit_id",
            )
        unit_ids.add(unit.unit_id)
        key = (unit.turn_index, unit.unit_index)
        if key in unit_keys:
            raise BusinessError(
                "E_INPUT_INVALID",
                "duplicate (turn_index, unit_index) pair",
                session_id=session.session_id,
                field="units.unit_index",
            )
        unit_keys.add(key)
        if unit.turn_index not in known_turns:
            raise BusinessError(
                "E_INPUT_INVALID",
                f"unit {unit.unit_id!r} references unknown turn {unit.turn_index}",
                session_id=session.session_id,
                field="units.turn_index",
            )
    if not session.units:
        raise BusinessError(
            "E_INPUT_INVALID",
            "session must contain at least one summary unit",
            session_id=session.session_id,
            field="units",
        )
    if session.constraints.max_chars <= 0:
        raise BusinessError(
            "E_INPUT_INVALID",
            "max_chars must be positive",
            session_id=session.session_id,
            field="rewrite_constraints.max_chars",
        )


def validate_vocabulary_usage(
    sessions: Sequence[Session], vocabulary: Vocabulary
) -> None:
    for session in sessions:
        for turn in session.turns:
            tokenize_text(
                turn.text,
                vocabulary,
                session_id=session.session_id,
                turn_index=turn.turn_index,
                field=f"turns[{turn.turn_index}].text",
            )
        for unit in session.units:
            tokenize_text(
                unit.text,
                vocabulary,
                session_id=session.session_id,
                turn_index=unit.turn_index,
                field=f"units[{unit.unit_id}].text",
            )
        for word in session.constraints.forbidden_words:
            if word and not any(word in token for token in vocabulary.words):
                raise BusinessError(
                    "E_VOCAB_MISSING",
                    f"forbidden word {word!r} is absent from vocabulary",
                    session_id=session.session_id,
                    field="rewrite_constraints.forbidden_words",
                    details={"vocab_version": vocabulary.vocab_version, "word": word},
                )


def resource_guard(
    sessions: Sequence[Session], vocabulary: Vocabulary, config: SummaryConfig
) -> List[Dict[str, Any]]:
    """Check configured resource limits and return degradation records."""

    degradations: List[Dict[str, Any]] = []
    if len(sessions) > config.max_sessions_per_stage:
        record = {
            "category": "session_count",
            "observed": len(sessions),
            "limit": config.max_sessions_per_stage,
        }
        if config.resource_policy == "error":
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "session count exceeds max_sessions_per_stage",
                field="max_sessions_per_stage",
                details=record,
            )
        degradations.append(record)

    token_count = 0
    unit_count = 0
    unique_terms: set[str] = set()
    for session in sessions:
        for turn in session.turns:
            words = tokenize_text(
                turn.text,
                vocabulary,
                session_id=session.session_id,
                turn_index=turn.turn_index,
                field=f"turns[{turn.turn_index}].text",
            )
            token_count += len(words)
            unique_terms.update(words)
        for unit in session.units:
            words = tokenize_text(
                unit.text,
                vocabulary,
                session_id=session.session_id,
                turn_index=unit.turn_index,
                field=f"units[{unit.unit_id}].text",
            )
            token_count += len(words)
            unit_count += 1

    if len(unique_terms) > config.max_resident_frequency:
        record = {
            "category": "resident_frequency",
            "observed": len(unique_terms),
            "limit": config.max_resident_frequency,
        }
        if config.resource_policy == "error":
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "resident frequency cardinality exceeds max_resident_frequency",
                field="max_resident_frequency",
                details=record,
            )
        degradations.append(record)

    estimated_bytes = token_count * 64 + unit_count * 512 + len(unique_terms) * 256
    if estimated_bytes > config.max_intermediate_memory_bytes:
        record = {
            "category": "intermediate_memory",
            "observed": estimated_bytes,
            "limit": config.max_intermediate_memory_bytes,
        }
        if config.resource_policy == "error":
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "estimated intermediate memory exceeds configured limit",
                field="max_intermediate_memory_bytes",
                details=record,
            )
        degradations.append(record)
    return degradations


def dataset_signature(
    sessions: Sequence[Session], vocabulary: Vocabulary, config: SummaryConfig
) -> str:
    from .canonical import sha256_value

    return sha256_value(
        {
            "sessions": [session.to_dict() for session in sessions],
            "vocabulary": vocabulary.to_dict(),
            "config": config.to_dict(),
        }
    )
