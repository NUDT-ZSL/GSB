from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError, PolicyError
from .io import canonical_json_bytes, read_jsonl, sha256_bytes, sha256_json


def _load_yaml_or_json(path: Path) -> tuple[Any, bytes]:
    data = path.read_bytes()
    try:
        if path.suffix.lower() == ".json":
            value = json.loads(data.decode("utf-8"))
        else:
            value = yaml.safe_load(data.decode("utf-8"))
    except Exception as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"top-level value in {path} must be a mapping")
    return value, data


def _require_int(mapping: dict[str, Any], key: str, *, minimum: int = 1, where: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where}.{key} must be an integer >= {minimum}, got {value!r}")
    return value


def _require_str(mapping: dict[str, Any], key: str, *, where: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}.{key} must be a non-empty string")
    return value


@dataclass(frozen=True)
class TensorSpec:
    name: str
    shape_template: tuple[str, ...]


@dataclass(frozen=True)
class TrainingConfig:
    raw: dict[str, Any]
    config_hash: str
    seed: int
    layers: int
    hidden_size: int
    vocab_size: int
    tensor_specs: tuple[TensorSpec, ...]
    workers: int
    shard_size_limit_bytes: int
    global_budget_bytes: int
    write_concurrency: int
    snapshot_commit_interval_rounds: int
    retention_generations: int
    total_rounds: int
    fixed_point_scale: int

    @classmethod
    def load(cls, path: Path) -> "TrainingConfig":
        raw, _ = _load_yaml_or_json(path)
        schema_version = raw.get("schema_version", 1)
        if schema_version != 1:
            raise ConfigError(f"unsupported training config schema_version={schema_version!r}")

        model = raw.get("model")
        dp = raw.get("data_parallel")
        storage = raw.get("storage")
        training = raw.get("training", {})
        if not isinstance(model, dict) or not isinstance(dp, dict) or not isinstance(storage, dict):
            raise ConfigError("model, data_parallel and storage mappings are required")
        if not isinstance(training, dict):
            raise ConfigError("training must be a mapping")

        seed = _require_int(raw, "seed", minimum=0, where="root")
        layers = _require_int(model, "layers", where="model")
        hidden_size = _require_int(model, "hidden_size", where="model")
        vocab_size = _require_int(model, "vocab_size", where="model")

        raw_specs = model.get("parameter_tensors")
        if not isinstance(raw_specs, list) or not raw_specs:
            raise ConfigError("model.parameter_tensors must be a non-empty list")
        specs: list[TensorSpec] = []
        names: set[str] = set()
        for index, item in enumerate(raw_specs):
            if not isinstance(item, dict):
                raise ConfigError(f"model.parameter_tensors[{index}] must be a mapping")
            name = _require_str(item, "name", where=f"model.parameter_tensors[{index}]")
            shape = item.get("shape")
            if not isinstance(shape, list) or not shape:
                raise ConfigError(f"model.parameter_tensors[{index}].shape must be a non-empty list")
            shape_template: list[str] = []
            for dim in shape:
                if isinstance(dim, int) and not isinstance(dim, bool):
                    if dim < 1:
                        raise ConfigError(f"tensor {name} has a non-positive dimension")
                    shape_template.append(str(dim))
                elif isinstance(dim, str) and dim:
                    shape_template.append(dim)
                else:
                    raise ConfigError(f"tensor {name} has invalid dimension {dim!r}")
            if name in names:
                raise ConfigError(f"duplicate tensor template name {name!r}")
            names.add(name)
            specs.append(TensorSpec(name=name, shape_template=tuple(shape_template)))

        workers = _require_int(dp, "workers", where="data_parallel")
        shard_limit = _require_int(dp, "shard_size_limit_bytes", where="data_parallel")
        write_concurrency = _require_int(dp, "write_concurrency", where="data_parallel")
        interval = _require_int(dp, "snapshot_commit_interval_rounds", where="data_parallel")
        retention = _require_int(dp, "retention_generations", where="data_parallel")
        total_rounds = _require_int(dp, "total_rounds", where="data_parallel")
        budget = _require_int(storage, "global_budget_bytes", where="storage")
        fixed_scale = training.get("fixed_point_scale", 1_000_000)
        if not isinstance(fixed_scale, int) or isinstance(fixed_scale, bool) or fixed_scale < 1:
            raise ConfigError("training.fixed_point_scale must be a positive integer")
        if shard_limit % 8 != 0:
            raise ConfigError("data_parallel.shard_size_limit_bytes must be divisible by 8")
        if write_concurrency < 1 or workers < 1:
            raise ConfigError("worker and write concurrency counts must be positive")

        return cls(
            raw=raw,
            config_hash=sha256_json(raw),
            seed=seed,
            layers=layers,
            hidden_size=hidden_size,
            vocab_size=vocab_size,
            tensor_specs=tuple(specs),
            workers=workers,
            shard_size_limit_bytes=shard_limit,
            global_budget_bytes=budget,
            write_concurrency=write_concurrency,
            snapshot_commit_interval_rounds=interval,
            retention_generations=retention,
            total_rounds=total_rounds,
            fixed_point_scale=fixed_scale,
        )


@dataclass(frozen=True)
class DataShard:
    shard_id: str
    sample_count: int
    feature_digest: str
    label_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "shard_id": self.shard_id,
            "sample_count": self.sample_count,
            "feature_digest": self.feature_digest,
            "label_digest": self.label_digest,
        }


def load_data_shards(path: Path) -> list[DataShard]:
    try:
        rows = read_jsonl(path)
    except Exception as exc:
        raise ConfigError(f"cannot read data shard description {path}: {exc}") from exc
    if not rows:
        raise ConfigError("data shard description is empty")
    shards: list[DataShard] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigError(f"data shard row {index + 1} must be an object")
        shard_id = row.get("shard_id", row.get("id"))
        if not isinstance(shard_id, str) or not shard_id:
            raise ConfigError(f"data shard row {index + 1} has no shard_id")
        if shard_id in seen:
            raise ConfigError(f"duplicate data shard id {shard_id!r}")
        seen.add(shard_id)
        sample_count = row.get("sample_count")
        if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 1:
            raise ConfigError(f"data shard {shard_id} has invalid sample_count")
        feature_digest = row.get("feature_digest")
        label_digest = row.get("label_digest")
        if not isinstance(feature_digest, str) or len(feature_digest) < 8:
            raise ConfigError(f"data shard {shard_id} has invalid feature_digest")
        if not isinstance(label_digest, str) or len(label_digest) < 8:
            raise ConfigError(f"data shard {shard_id} has invalid label_digest")
        shards.append(DataShard(shard_id, sample_count, feature_digest, label_digest))
    return shards


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    occupancy_min: float
    occupancy_max: float
    remaining_rounds_min: int
    remaining_rounds_max: int
    retention_generations: int
    merge_mode: str
    merge_small_threshold_bytes: int
    max_merged_bytes: int
    write_priority: str
    budget_action: str

    def matches(self, occupancy: float, remaining_rounds: int) -> bool:
        upper_ok = occupancy < self.occupancy_max or (
            self.occupancy_max >= 1.0 and occupancy <= self.occupancy_max
        )
        return (
            occupancy >= self.occupancy_min
            and upper_ok
            and remaining_rounds >= self.remaining_rounds_min
            and remaining_rounds <= self.remaining_rounds_max
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "occupancy_min": self.occupancy_min,
            "occupancy_max": self.occupancy_max,
            "remaining_rounds_min": self.remaining_rounds_min,
            "remaining_rounds_max": self.remaining_rounds_max,
            "retention_generations": self.retention_generations,
            "merge_mode": self.merge_mode,
            "merge_small_threshold_bytes": self.merge_small_threshold_bytes,
            "max_merged_bytes": self.max_merged_bytes,
            "write_priority": self.write_priority,
            "budget_action": self.budget_action,
        }


@dataclass(frozen=True)
class PolicyTable:
    raw: dict[str, Any]
    policy_hash: str
    rules: tuple[PolicyRule, ...]

    @classmethod
    def load(cls, path: Path) -> "PolicyTable":
        raw, _ = _load_yaml_or_json(path)
        if raw.get("schema_version", 1) != 1:
            raise PolicyError("unsupported policy schema_version")
        raw_rules = raw.get("rules")
        if not isinstance(raw_rules, list) or not raw_rules:
            raise PolicyError("policy rules must be a non-empty list")
        rules: list[PolicyRule] = []
        ids: set[str] = set()
        for index, raw_rule in enumerate(raw_rules):
            if not isinstance(raw_rule, dict):
                raise PolicyError(f"policy rule {index + 1} must be a mapping")
            rule_id = raw_rule.get("id", raw_rule.get("rule_id"))
            if not isinstance(rule_id, str) or not rule_id:
                raise PolicyError(f"policy rule {index + 1} has no id")
            if rule_id in ids:
                raise PolicyError(f"duplicate policy rule id {rule_id!r}")
            ids.add(rule_id)
            when = raw_rule.get("when", {})
            actions = raw_rule.get("actions", raw_rule)
            if not isinstance(when, dict) or not isinstance(actions, dict):
                raise PolicyError(f"policy rule {rule_id} has invalid when/actions")
            occ = when.get("occupancy", {})
            rem = when.get("remaining_rounds", {})
            if not isinstance(occ, dict) or not isinstance(rem, dict):
                raise PolicyError(f"policy rule {rule_id} has invalid ranges")
            try:
                occ_min = float(occ.get("min", raw_rule.get("occupancy_min", 0.0)))
                occ_max = float(occ.get("max", raw_rule.get("occupancy_max", 1.0)))
                rem_min = int(rem.get("min", raw_rule.get("remaining_rounds_min", 0)))
                rem_max = int(rem.get("max", raw_rule.get("remaining_rounds_max", 1_000_000)))
                retention = int(actions.get("retention_generations", 1))
                merge_mode = str(actions.get("merge_mode", "none"))
                threshold = int(actions.get("merge_small_threshold_bytes", 0))
                max_merged = int(actions.get("max_merged_bytes", 0))
                priority = str(actions.get("write_priority", "shard_id_asc"))
                budget_action = str(actions.get("budget_action", "reject"))
            except (TypeError, ValueError) as exc:
                raise PolicyError(f"policy rule {rule_id} has invalid action values: {exc}") from exc
            if not (0.0 <= occ_min < occ_max <= 1.0):
                raise PolicyError(f"policy rule {rule_id} has invalid occupancy range")
            if rem_min < 0 or rem_max < rem_min:
                raise PolicyError(f"policy rule {rule_id} has invalid remaining-round range")
            if merge_mode not in {"none", "merge_small"}:
                raise PolicyError(f"policy rule {rule_id} has unsupported merge_mode={merge_mode!r}")
            if priority not in {"tensor_size_desc", "shard_size_desc", "shard_id_asc", "worker_then_chunk"}:
                raise PolicyError(f"policy rule {rule_id} has unsupported write_priority={priority!r}")
            if budget_action not in {"evict_then_write", "reject"}:
                raise PolicyError(f"policy rule {rule_id} has unsupported budget_action={budget_action!r}")
            rules.append(
                PolicyRule(
                    rule_id=rule_id,
                    occupancy_min=occ_min,
                    occupancy_max=occ_max,
                    remaining_rounds_min=rem_min,
                    remaining_rounds_max=rem_max,
                    retention_generations=retention,
                    merge_mode=merge_mode,
                    merge_small_threshold_bytes=max(0, threshold),
                    max_merged_bytes=max(0, max_merged),
                    write_priority=priority,
                    budget_action=budget_action,
                )
            )
        return cls(raw=raw, policy_hash=sha256_json(raw), rules=tuple(rules))

    def select(self, occupancy: float, remaining_rounds: int) -> PolicyRule:
        for rule in self.rules:
            if rule.matches(occupancy, remaining_rounds):
                return rule
        raise PolicyError(
            "no policy rule matches occupancy={:.6f}, remaining_rounds={}".format(
                occupancy, remaining_rounds
            )
        )


@dataclass
class SampleBundle:
    config: TrainingConfig
    policy: PolicyTable
    data_shards: list[DataShard]
    data_hash: str
    injections_path: Path | None = None

    @classmethod
    def load(cls, samples_dir: Path, *, injections_path: Path | None = None) -> "SampleBundle":
        config = TrainingConfig.load(samples_dir / "train_config.yaml")
        policy = PolicyTable.load(samples_dir / "scheduler_policy.yaml")
        data_path = samples_dir / "data_shards.jsonl"
        shards = load_data_shards(data_path)
        data_hash = sha256_bytes(data_path.read_bytes())
        return cls(config, policy, shards, data_hash, injections_path)

    @property
    def input_hash(self) -> str:
        return sha256_bytes(
            b"".join(
                [
                    self.config.config_hash.encode("ascii"),
                    b":",
                    self.policy.policy_hash.encode("ascii"),
                    b":",
                    self.data_hash.encode("ascii"),
                ]
            )
        )

    def deterministic_inputs(self) -> dict[str, str]:
        return {
            "config_hash": self.config.config_hash,
            "policy_hash": self.policy.policy_hash,
            "data_hash": self.data_hash,
        }


def hash_file_bytes(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))
