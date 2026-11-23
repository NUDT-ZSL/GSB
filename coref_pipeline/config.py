"""Coreference rule configuration and evidence primitives."""

from __future__ import annotations

import difflib
import math
import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, content_hash, q6, read_json
from .errors import E_EVIDENCE_CONFIG, PipelineError


SUPPORTED_TRIGGER_METHODS = {
    "exact",
    "char_bigram_jaccard",
    "sequence_matcher",
    "normalized_levenshtein",
}
SUPPORTED_CHAIN_PRIORITIES = ("evidence_total", "chain_size", "first_seen")


def _config_error(field: str, message: str) -> PipelineError:
    return PipelineError(
        code=E_EVIDENCE_CONFIG,
        stage="evidence_config",
        field=field,
        message=message,
    )


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _config_error(field, "必须为有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise _config_error(field, "必须为有限数值")
    return number


def _version(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _config_error(field, "版本号必须为大于等于 1 的整数")
    return value


def normalize_text(value: str, *, casefold: bool = True) -> str:
    value = unicodedata.normalize("NFC", value.strip())
    if casefold:
        value = value.casefold()
    return " ".join(value.split())


class RulesConfig:
    """Validated, immutable view over the rule JSON document."""

    def __init__(self, payload: dict[str, Any]):
        if not isinstance(payload, dict):
            raise _config_error("$", "配置根节点必须为对象")
        self.payload = payload
        self._validate()
        self.config_hash = content_hash(payload)

    @classmethod
    def load(cls, path: str | Path) -> "RulesConfig":
        path = Path(path)
        try:
            payload = read_json(path)
        except (OSError, ValueError) as exc:
            raise PipelineError(
                code=E_EVIDENCE_CONFIG,
                stage="evidence_config",
                field="$",
                message=f"无法读取配置文件: {exc}",
            ) from exc
        return cls(payload)

    def _validate(self) -> None:
        payload = self.payload
        weights = payload.get("weights")
        if not isinstance(weights, dict):
            raise _config_error("weights", "必须为对象")
        _version(weights.get("version"), "weights.version")
        for key in ("entity", "time", "trigger", "source_trust"):
            value = _number(weights.get(key), f"weights.{key}")
            if value < 0:
                raise _config_error(f"weights.{key}", "权重不得为负数")
        if sum(float(weights[k]) for k in ("entity", "time", "trigger", "source_trust")) <= 0:
            raise _config_error("weights", "至少一个权重必须大于 0")

        role_weights = payload.get("entity_role_weights")
        if not isinstance(role_weights, dict) or not role_weights:
            raise _config_error("entity_role_weights", "必须为非空对象")
        for role, value in role_weights.items():
            if not isinstance(role, str) or not role.strip():
                raise _config_error("entity_role_weights", "角色名不得为空")
            role_weight = _number(value, f"entity_role_weights.{role}")
            if role_weight < 0 or role_weight > 1:
                raise _config_error(
                    f"entity_role_weights.{role}", "角色权重必须位于 [0, 1]"
                )

        time_rules = payload.get("time_equivalence")
        if not isinstance(time_rules, dict):
            raise _config_error("time_equivalence", "必须为对象")
        _version(time_rules.get("version"), "time_equivalence.version")
        groups = time_rules.get("groups")
        if not isinstance(groups, list):
            raise _config_error("time_equivalence.groups", "必须为数组")
        seen_aliases: dict[str, int] = {}
        for idx, group in enumerate(groups):
            if not isinstance(group, list) or len(group) < 2:
                raise _config_error(
                    f"time_equivalence.groups[{idx}]", "每组至少包含两个等价表达式"
                )
            for alias in group:
                if not isinstance(alias, str) or not alias.strip():
                    raise _config_error(
                        f"time_equivalence.groups[{idx}]", "等价表达式不得为空"
                    )
                normalized = normalize_text(alias)
                previous = seen_aliases.get(normalized)
                if previous is not None and previous != idx:
                    raise _config_error(
                        f"time_equivalence.groups[{idx}]",
                        "同一表达式不得出现在多个等价组",
                    )
                seen_aliases[normalized] = idx

        trigger = payload.get("trigger_similarity")
        if not isinstance(trigger, dict):
            raise _config_error("trigger_similarity", "必须为对象")
        _version(trigger.get("version"), "trigger_similarity.version")
        method = trigger.get("method")
        if method not in SUPPORTED_TRIGGER_METHODS:
            raise _config_error(
                "trigger_similarity.method",
                f"不支持的口径: {method!r}",
            )
        casefold = trigger.get("casefold", True)
        if not isinstance(casefold, bool):
            raise _config_error("trigger_similarity.casefold", "必须为布尔值")

        floor = _number(payload.get("candidate_floor"), "candidate_floor")
        weight_total = sum(
            float(weights[k]) for k in ("entity", "time", "trigger", "source_trust")
        )
        if floor < 0 or floor > weight_total + 1e-9:
            raise _config_error(
                "candidate_floor", "候选下限必须位于 [0, 权重总和]"
            )

        priority = payload.get("chain_priority")
        if (
            not isinstance(priority, list)
            or sorted(priority) != sorted(SUPPORTED_CHAIN_PRIORITIES)
        ):
            raise _config_error(
                "chain_priority",
                "必须且只能按任意顺序包含 evidence_total、chain_size、first_seen",
            )

        limits = payload.get("resource_limits")
        if not isinstance(limits, dict):
            raise _config_error("resource_limits", "必须为对象")
        for key in (
            "max_batch_events",
            "max_pair_comparisons",
            "max_resident_chain_mb",
        ):
            value = _number(limits.get(key), f"resource_limits.{key}")
            if value <= 0:
                raise _config_error(f"resource_limits.{key}", "上限必须大于 0")

        mode = payload.get("resource_limit_mode", "error")
        if mode not in {"error", "degrade"}:
            raise _config_error(
                "resource_limit_mode", "必须为 error 或 degrade"
            )
        degraded_floor = _number(
            payload.get("degraded_candidate_floor", floor),
            "degraded_candidate_floor",
        )
        if degraded_floor < floor or degraded_floor > weight_total + 1e-9:
            raise _config_error(
                "degraded_candidate_floor",
                "降级下限必须不低于 candidate_floor 且不超过权重总和",
            )

    @property
    def weights(self) -> dict[str, Any]:
        return self.payload["weights"]

    @property
    def role_weights(self) -> dict[str, float]:
        return {
            role: float(value)
            for role, value in self.payload["entity_role_weights"].items()
        }

    @property
    def candidate_floor(self) -> float:
        return float(self.payload["candidate_floor"])

    @property
    def degraded_floor(self) -> float:
        return float(self.payload.get("degraded_candidate_floor", self.candidate_floor))

    @property
    def chain_priority(self) -> list[str]:
        return list(self.payload["chain_priority"])

    @property
    def limits(self) -> dict[str, float]:
        return {
            key: float(value)
            for key, value in self.payload["resource_limits"].items()
        }

    @property
    def resource_limit_mode(self) -> str:
        return str(self.payload.get("resource_limit_mode", "error"))

    @property
    def weights_version(self) -> int:
        return int(self.weights["version"])

    @property
    def time_rules_version(self) -> int:
        return int(self.payload["time_equivalence"]["version"])

    @property
    def trigger_version(self) -> int:
        return int(self.payload["trigger_similarity"]["version"])

    def snapshot_components(self) -> dict[str, dict[str, Any]]:
        """Components that must be independently versioned in snapshots."""

        return {
            "evidence_weights": {
                "version": self.weights_version,
                "payload": self.weights,
            },
            "time_equivalence": {
                "version": self.time_rules_version,
                "payload": self.payload["time_equivalence"],
            },
            "trigger_similarity": {
                "version": self.trigger_version,
                "payload": self.payload["trigger_similarity"],
            },
        }

    def equivalence_inputs_hash(self) -> str:
        return content_hash(
            {
                "weights_version": self.weights_version,
                "time_rules_version": self.time_rules_version,
                "trigger_version": self.trigger_version,
                "candidate_floor": self.candidate_floor,
            }
        )


class TimeEquivalence:
    def __init__(self, config: RulesConfig):
        self.version = config.time_rules_version
        rules = config.payload["time_equivalence"]
        self._canonical: dict[str, str] = {}
        self._group_id: dict[str, int] = {}
        for idx, group in enumerate(rules["groups"]):
            canonical = normalize_text(group[0])
            for alias in group:
                normalized = normalize_text(alias)
                self._canonical[normalized] = canonical
                self._group_id[normalized] = idx

    def equivalent(self, left: str, right: str) -> tuple[bool, str]:
        left_norm = normalize_text(left)
        right_norm = normalize_text(right)
        left_canonical = self._canonical.get(left_norm)
        right_canonical = self._canonical.get(right_norm)
        if left_canonical is not None and right_canonical is not None:
            if left_canonical == right_canonical:
                return True, f"time_group:{self._group_id[left_norm]}"
        if left_norm == right_norm:
            return True, "normalized_exact"
        return False, "none"


def _bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[idx : idx + 2] for idx in range(len(text) - 1)}


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, 1):
        current = [i]
        for j, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[j] + 1,
                    previous[j - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def trigger_similarity(left: str, right: str, config: RulesConfig) -> float:
    trigger_config = config.payload["trigger_similarity"]
    method = trigger_config["method"]
    casefold = bool(trigger_config.get("casefold", True))
    a = normalize_text(left, casefold=casefold)
    b = normalize_text(right, casefold=casefold)
    if method == "exact":
        return 1.0 if a == b else 0.0
    if method == "char_bigram_jaccard":
        grams_a = _bigrams(a)
        grams_b = _bigrams(b)
        if not grams_a and not grams_b:
            return 1.0
        if not grams_a or not grams_b:
            return 0.0
        return q6(len(grams_a & grams_b) / len(grams_a | grams_b))
    if method == "sequence_matcher":
        if not a and not b:
            return 1.0
        return q6(difflib.SequenceMatcher(None, a, b, autojunk=False).ratio())
    if method == "normalized_levenshtein":
        if not a and not b:
            return 1.0
        return q6(1.0 - (_levenshtein(a, b) / max(len(a), len(b))))
    raise AssertionError(f"validated method not implemented: {method}")


def validate_roles(roles: Iterable[str], role_weights: dict[str, float]) -> None:
    for role in roles:
        if role not in role_weights:
            raise PipelineError(
                code="E_ROLE_INVALID",
                stage="ingest",
                field="entities.role",
                message=f"实体角色不在受控角色表中: {role!r}",
                details={"role": role},
            )

