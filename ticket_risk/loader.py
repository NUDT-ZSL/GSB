"""JSONL sample, rule-pack, and configuration loading with validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import (
    E_BAD_RECORD,
    E_EMPTY_TEXT,
    E_RULE_CYCLE,
    E_UNKNOWN_CHANNEL,
    Failure,
    LoadBundle,
)
from .util import require_finite_float, sha256_file, sha256_obj


RISK_LEVELS = ("一般", "催办", "投诉", "风险")
RISK_ORDER = {level: index for index, level in enumerate(RISK_LEVELS)}


@dataclass(frozen=True)
class Sample:
    sample_id: str
    text: str
    channel: str
    line: int

    def content_fingerprint(self) -> str:
        return sha256_obj(
            {"sample_id": self.sample_id, "text": self.text, "channel": self.channel}
        )


class _DuplicateKey(ValueError):
    pass


class RuleMappingError(ValueError):
    """Malformed or cyclic risk-keyword/level mapping."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_json_text(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_no_duplicate_keys)


def _load_json_file(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return _parse_json_text(handle.read())


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _require_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def validate_config(raw: Any) -> dict[str, Any]:
    config = _require_mapping(raw, "config")
    channels = config.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("channels must be a non-empty array")
    normalized_channels = [_require_string(v, "channel") for v in channels]
    if len(set(normalized_channels)) != len(normalized_channels):
        raise ValueError("channels must be unique")

    lexicon_scale = require_finite_float(
        config.get("lexicon_weight_scale"), "lexicon_weight_scale"
    )
    risk_scale = require_finite_float(
        config.get("risk_weight_scale"), "risk_weight_scale"
    )
    position_decay = require_finite_float(config.get("position_decay"), "position_decay")
    fusion_temperature = require_finite_float(
        config.get("fusion_temperature"), "fusion_temperature"
    )
    budget_ms = require_finite_float(
        config.get("interaction_budget_ms"), "interaction_budget_ms"
    )
    negation_window = config.get("negation_window")
    worker_threads = config.get("worker_threads")
    snapshot_interval = config.get("snapshot_commit_interval")

    if lexicon_scale < 0 or risk_scale < 0:
        raise ValueError("weight scales must be non-negative")
    if not 0 <= position_decay <= 1:
        raise ValueError("position_decay must be in [0, 1]")
    if fusion_temperature <= 0:
        raise ValueError("fusion_temperature must be positive")
    if budget_ms < 0:
        raise ValueError("interaction_budget_ms must be non-negative")
    if isinstance(negation_window, bool) or not isinstance(negation_window, int):
        raise ValueError("negation_window must be an integer")
    if negation_window < 0:
        raise ValueError("negation_window must be non-negative")
    if isinstance(worker_threads, bool) or not isinstance(worker_threads, int):
        raise ValueError("worker_threads must be an integer")
    if worker_threads < 1:
        raise ValueError("worker_threads must be at least 1")
    if isinstance(snapshot_interval, bool) or not isinstance(snapshot_interval, int):
        raise ValueError("snapshot_commit_interval must be an integer")
    if snapshot_interval < 1:
        raise ValueError("snapshot_commit_interval must be at least 1")

    threshold_raw = _require_mapping(
        config.get("risk_level_thresholds"), "risk_level_thresholds"
    )
    if set(threshold_raw) != set(RISK_LEVELS):
        raise ValueError("risk_level_thresholds must contain exactly the four levels")
    thresholds = {
        level: require_finite_float(threshold_raw[level], f"threshold.{level}")
        for level in RISK_LEVELS
    }
    if any(not 0 <= value <= 1 for value in thresholds.values()):
        raise ValueError("risk thresholds must be in [0, 1]")
    if list(thresholds.values()) != sorted(thresholds.values()):
        raise ValueError("risk thresholds must be non-decreasing by level order")

    return {
        "schema_version": config.get("schema_version", 1),
        "channels": normalized_channels,
        "lexicon_weight_scale": lexicon_scale,
        "risk_weight_scale": risk_scale,
        "position_decay": position_decay,
        "negation_window": negation_window,
        "risk_level_thresholds": thresholds,
        "fusion_temperature": fusion_temperature,
        "interaction_budget_ms": budget_ms,
        "worker_threads": worker_threads,
        "snapshot_commit_interval": snapshot_interval,
    }


def _deduplicate_entries(
    raw: Any,
    section_name: str,
    key_field: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError(f"{section_name} must be an array")
    result: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(raw):
        entry = _require_mapping(item, f"{section_name}[{index}]")
        key = _require_string(entry.get(key_field), f"{section_name}[{index}].{key_field}")
        if key in result:
            raise ValueError(f"duplicate {key_field} in {section_name}: {key}")
        result[key] = entry
    return result


def _validate_level_cycles(level_map: dict[str, dict[str, Any]]) -> None:
    missing = set(RISK_LEVELS) - set(level_map)
    extra = set(level_map) - set(RISK_LEVELS)
    if missing or extra:
        raise RuleMappingError(
            f"risk_level_map levels mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
        )
    state: dict[str, int] = {}

    def visit(level: str, path: tuple[str, ...]) -> None:
        mark = state.get(level, 0)
        if mark == 1:
            cycle = " -> ".join(path + (level,))
            raise RuleMappingError(f"risk level mapping cycle: {cycle}")
        if mark == 2:
            return
        state[level] = 1
        parent = level_map[level].get("parent")
        if parent not in (None, ""):
            try:
                parent = _require_string(parent, f"risk_level_map.{level}.parent")
            except ValueError as exc:
                raise RuleMappingError(
                    f"risk level mapping parent is invalid for {level}: {exc}"
                ) from exc
            if parent not in level_map:
                raise RuleMappingError(f"risk level mapping points to unknown parent: {parent}")
            visit(parent, path + (level,))
        state[level] = 2

    for level in RISK_LEVELS:
        visit(level, ())


def validate_resources(raw: Any) -> dict[str, Any]:
    resources = _require_mapping(raw, "rules")
    sentiment_raw = _deduplicate_entries(
        resources.get("sentiment_lexicon"), "sentiment_lexicon", "word"
    )
    sentiment_lexicon: dict[str, dict[str, Any]] = {}
    for word, entry in sentiment_raw.items():
        weight = require_finite_float(entry.get("weight"), f"sentiment_lexicon.{word}.weight")
        if not -1 <= weight <= 1:
            raise ValueError(f"sentiment weight must be in [-1, 1]: {word}")
        sentiment_lexicon[word] = {"weight": weight}

    degree_raw = _deduplicate_entries(resources.get("degree_adverbs"), "degree_adverbs", "word")
    degree_adverbs: dict[str, dict[str, Any]] = {}
    for word, entry in degree_raw.items():
        factor = require_finite_float(entry.get("factor"), f"degree_adverbs.{word}.factor")
        if factor < 0:
            raise ValueError(f"degree factor must be non-negative: {word}")
        degree_adverbs[word] = {"factor": factor}

    negation_words_raw = resources.get("negation_words")
    if not isinstance(negation_words_raw, list):
        raise ValueError("negation_words must be an array")
    negation_words = sorted(
        {_require_string(word, "negation_words[]") for word in negation_words_raw}
    )

    risk_keyword_raw = _deduplicate_entries(
        resources.get("risk_keywords"), "risk_keywords", "keyword"
    )
    risk_keywords: dict[str, dict[str, Any]] = {}
    for keyword, entry in risk_keyword_raw.items():
        try:
            level = _require_string(entry.get("level"), f"risk_keywords.{keyword}.level")
        except ValueError as exc:
            raise RuleMappingError(
                f"risk keyword mapping is invalid for {keyword}: {exc}"
            ) from exc
        if level not in RISK_LEVELS:
            raise RuleMappingError(f"risk keyword maps to unknown level: {keyword} -> {level}")
        weight = require_finite_float(entry.get("weight"), f"risk_keywords.{keyword}.weight")
        if weight < 0:
            raise ValueError(f"risk keyword weight must be non-negative: {keyword}")
        risk_keywords[keyword] = {"level": level, "weight": weight}

    level_map_raw = _require_mapping(resources.get("risk_level_map"), "risk_level_map")
    level_map: dict[str, dict[str, Any]] = {}
    for level, entry_raw in level_map_raw.items():
        if level not in RISK_LEVELS:
            raise ValueError(f"unknown level in risk_level_map: {level}")
        entry = _require_mapping(entry_raw, f"risk_level_map.{level}")
        weight = require_finite_float(entry.get("weight"), f"risk_level_map.{level}.weight")
        if weight < 0:
            raise ValueError(f"risk level weight must be non-negative: {level}")
        level_map[level] = {"weight": weight, "parent": entry.get("parent")}
    _validate_level_cycles(level_map)

    conflict_raw = resources.get("conflict_rules")
    if not isinstance(conflict_raw, list):
        raise ValueError("conflict_rules must be an array")
    conflict_rules: list[dict[str, str]] = []
    seen_conflicts: set[tuple[str, str]] = set()
    for index, item in enumerate(conflict_raw):
        entry = _require_mapping(item, f"conflict_rules[{index}]")
        sentiment = _require_string(entry.get("sentiment"), "conflict.sentiment")
        level = _require_string(entry.get("risk_level"), "conflict.risk_level")
        winner = _require_string(entry.get("winner"), "conflict.winner")
        resolved_level = _require_string(entry.get("resolved_level"), "conflict.resolved_level")
        reason = _require_string(entry.get("reason"), "conflict.reason")
        if sentiment not in {"positive", "negative"}:
            raise ValueError(f"invalid conflict sentiment: {sentiment}")
        if level not in RISK_LEVELS or resolved_level not in RISK_LEVELS:
            raise RuleMappingError("conflict rule references an unknown risk level")
        if winner not in {"risk", "sentiment"}:
            raise ValueError(f"invalid conflict winner: {winner}")
        pair = (sentiment, level)
        if pair in seen_conflicts:
            raise ValueError(f"duplicate conflict pair: {pair}")
        seen_conflicts.add(pair)
        conflict_rules.append(
            {
                "sentiment": sentiment,
                "risk_level": level,
                "winner": winner,
                "resolved_level": resolved_level,
                "reason": reason,
            }
        )
    conflict_rules.sort(key=lambda item: (item["sentiment"], item["risk_level"]))

    return {
        "schema_version": resources.get("schema_version", 1),
        "sentiment_lexicon": dict(sorted(sentiment_lexicon.items())),
        "degree_adverbs": dict(sorted(degree_adverbs.items())),
        "negation_words": negation_words,
        "risk_keywords": dict(sorted(risk_keywords.items())),
        "risk_level_map": {level: level_map[level] for level in RISK_LEVELS},
        "conflict_rules": conflict_rules,
    }


def _failure(
    code: str,
    detail: str,
    *,
    sample_id: str | None = None,
    line: int | None = None,
    scope: str = "sample",
) -> Failure:
    return Failure(
        business_code=code,
        detail=detail,
        sample_id=sample_id,
        line=line,
        scope=scope,
    )


def load_inputs(
    samples_path: str | Path,
    rules_path: str | Path,
    config_path: str | Path,
) -> LoadBundle:
    failures: list[Failure] = []
    samples: list[Sample] = []
    rules: dict[str, Any] = {}
    config: dict[str, Any] = {}
    fatal = False

    try:
        raw_config = _load_json_file(config_path)
        config = validate_config(raw_config)
    except Exception as exc:
        failures.append(
            _failure(
                E_BAD_RECORD,
                f"configuration load failed: {exc}",
                scope="config",
            )
        )
        fatal = True

    try:
        raw_rules = _load_json_file(rules_path)
        rules = validate_resources(raw_rules)
    except RuleMappingError as exc:
        code = E_RULE_CYCLE
        failures.append(_failure(code, f"rule pack load failed: {exc}", scope="rules"))
        fatal = True
    except ValueError as exc:
        code = E_BAD_RECORD
        failures.append(_failure(code, f"rule pack load failed: {exc}", scope="rules"))
        fatal = True
    except Exception as exc:
        failures.append(
            _failure(E_BAD_RECORD, f"rule pack load failed: {exc}", scope="rules")
        )
        fatal = True

    seen_ids: set[str] = set()
    if not config.get("channels"):
        config_channels: set[str] = set()
    else:
        config_channels = set(config["channels"])

    try:
        with open(samples_path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        sample_file_fingerprint = sha256_file(samples_path)
    except OSError as exc:
        failures.append(
            _failure(E_BAD_RECORD, f"sample file read failed: {exc}", scope="samples")
        )
        lines = []
        sample_file_fingerprint = ""
        fatal = True

    if not lines:
        failures.append(
            _failure(E_BAD_RECORD, "sample file is empty", scope="samples")
        )
        fatal = True

    for line_number, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            failures.append(
                _failure(E_BAD_RECORD, "blank JSONL line", line=line_number)
            )
            continue
        try:
            record = _parse_json_text(raw_line)
        except Exception as exc:
            failures.append(
                _failure(E_BAD_RECORD, f"invalid JSON: {exc}", line=line_number)
            )
            continue
        if not isinstance(record, dict):
            failures.append(
                _failure(E_BAD_RECORD, "JSONL record must be an object", line=line_number)
            )
            continue
        raw_id = record.get("id", record.get("sample_id"))
        if not isinstance(raw_id, str) or not raw_id.strip():
            failures.append(
                _failure(E_BAD_RECORD, "missing or invalid sample id", line=line_number)
            )
            continue
        sample_id = raw_id.strip()
        if sample_id in seen_ids:
            failures.append(
                _failure(
                    E_BAD_RECORD,
                    f"duplicate sample id: {sample_id}",
                    sample_id=sample_id,
                    line=line_number,
                )
            )
            continue
        seen_ids.add(sample_id)

        raw_text = record.get("text", record.get("sentence"))
        if not isinstance(raw_text, str):
            failures.append(
                _failure(
                    E_BAD_RECORD,
                    "text must be a string",
                    sample_id=sample_id,
                    line=line_number,
                )
            )
            continue
        if not raw_text.strip():
            failures.append(
                _failure(
                    E_EMPTY_TEXT,
                    "sample text is empty",
                    sample_id=sample_id,
                    line=line_number,
                )
            )
            continue
        channel = record.get("channel")
        if not isinstance(channel, str) or channel not in config_channels:
            failures.append(
                _failure(
                    E_UNKNOWN_CHANNEL,
                    f"unknown channel: {channel!r}",
                    sample_id=sample_id,
                    line=line_number,
                )
            )
            continue
        samples.append(
            Sample(
                sample_id=sample_id,
                text=raw_text,
                channel=channel,
                line=line_number,
            )
        )

    if not samples and lines:
        failures.append(
            _failure(E_BAD_RECORD, "no valid samples loaded", scope="samples")
        )
        fatal = True

    samples.sort(key=lambda item: item.sample_id)
    failures.sort(key=lambda item: item.sort_key())
    return LoadBundle(
        samples=samples,
        rules=rules,
        config=config,
        failures=failures,
        resource_fingerprint=sha256_obj(rules) if rules else "",
        config_hash=sha256_obj(config) if config else "",
        samples_fingerprint=sample_file_fingerprint,
        fatal=fatal,
    )
