"""Content-addressed four-node cache and dependency-aware invalidation plans."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import atomic_write_json, read_json_strict_no_duplicates, sha256_json
from .errors import corrupt
from .models import InferenceRequest, ThresholdConfig


PIPELINE_NODES = ("N1_FEATURE", "N2_SIMILARITY", "N3_CANDIDATE", "N4_FUSION")
NODE_INDEX = {node: index + 1 for index, node in enumerate(PIPELINE_NODES)}
CACHE_FORMAT_VERSION = 1
OPERATION_CONTRACT = "mm-route-contract-v1"
GENESIS_HASH = "0" * 64


def threshold_cache_token(config: ThresholdConfig) -> str:
    return sha256_json(
        {
            "generation": config.generation,
            "revision": config.revision,
            "thresholds": config.threshold_map(),
            "tombstones": [item.to_dict() for item in config.tombstones],
            "candidate_plan_count": config.candidate_plan_count,
        }
    )


def build_key_components(
    *,
    node: str,
    input_digest: str,
    upstream_artifact_hash: str,
    config: ThresholdConfig,
    precision: str,
) -> dict[str, Any]:
    if node not in NODE_INDEX:
        raise corrupt("unknown pipeline node", node=node)
    consumes_thresholds = node in {"N3_CANDIDATE", "N4_FUSION"}
    return {
        "cache_format_version": CACHE_FORMAT_VERSION,
        "operation_contract": OPERATION_CONTRACT,
        "node": node,
        "input_digest": input_digest,
        "upstream_artifact_hash": upstream_artifact_hash,
        "threshold_config_version": config.version_token if consumes_thresholds else "0.0",
        "threshold_config_hash": threshold_cache_token(config) if consumes_thresholds else "none",
        "route_budget": config.route_budget,
        "precision": precision,
    }


def cache_key(components: Mapping[str, Any]) -> str:
    return sha256_json(dict(components))


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    key: str
    key_components: dict[str, Any]
    artifact: dict[str, Any] | None
    artifact_hash: str | None


class ContentAddressedCache:
    """A process-safe object store: one immutable JSON object per node key."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise corrupt("cache key must be lowercase SHA-256", key=key)
        return self.objects / key[:2] / f"{key}.json"

    def get(
        self,
        key: str,
        *,
        key_components: Mapping[str, Any],
    ) -> CacheLookup:
        path = self._path(key)
        if not path.is_file():
            return CacheLookup(False, key, dict(key_components), None, None)
        envelope = read_json_strict_no_duplicates(path)
        if not isinstance(envelope, dict):
            raise corrupt("cache object is not JSON object", key=key)
        if envelope.get("cache_format_version") != CACHE_FORMAT_VERSION:
            raise corrupt("cache object format mismatch", key=key)
        if envelope.get("key") != key:
            raise corrupt("cache object key mismatch", key=key)
        if envelope.get("key_components") != dict(key_components):
            raise corrupt("cache key components mismatch", key=key)
        artifact = envelope.get("artifact")
        artifact_hash = envelope.get("artifact_hash")
        if not isinstance(artifact, dict) or not isinstance(artifact_hash, str):
            raise corrupt("cache object envelope is incomplete", key=key)
        actual_hash = sha256_json(
            {
                "node": envelope.get("node"),
                "artifact": artifact,
            }
        )
        if actual_hash != artifact_hash:
            raise corrupt(
                "cache artifact hash mismatch",
                key=key,
                expected=artifact_hash,
                actual=actual_hash,
            )
        return CacheLookup(True, key, dict(key_components), artifact, artifact_hash)

    def put(
        self,
        *,
        key: str,
        key_components: Mapping[str, Any],
        node: str,
        artifact: dict[str, Any],
    ) -> str:
        artifact_hash = sha256_json({"node": node, "artifact": artifact})
        envelope = {
            "cache_format_version": CACHE_FORMAT_VERSION,
            "key": key,
            "key_components": dict(key_components),
            "node": node,
            "artifact": artifact,
            "artifact_hash": artifact_hash,
        }
        atomic_write_json(self._path(key), envelope)
        return artifact_hash

    def keys(self) -> tuple[str, ...]:
        return tuple(
            sorted(path.stem for path in self.objects.glob("*/*.json") if path.is_file())
        )

    def stored_object(self, key: str) -> dict[str, Any]:
        path = self._path(key)
        value = read_json_strict_no_duplicates(path)
        if not isinstance(value, dict):
            raise corrupt("cache object is not a JSON object", key=key)
        return value

    def reset(self) -> None:
        for path in sorted(self.objects.glob("*/*.json")):
            path.unlink()


class TransactionalCache:
    """Read committed objects, stage new objects, and promote only after commit."""

    def __init__(
        self,
        committed_root: str | Path,
        staging_root: str | Path,
    ):
        self.committed = ContentAddressedCache(committed_root)
        self.staging = ContentAddressedCache(staging_root)

    def get(
        self,
        key: str,
        *,
        key_components: Mapping[str, Any],
    ) -> CacheLookup:
        staged = self.staging.get(key, key_components=key_components)
        if staged.hit:
            return staged
        return self.committed.get(key, key_components=key_components)

    def put(
        self,
        *,
        key: str,
        key_components: Mapping[str, Any],
        node: str,
        artifact: dict[str, Any],
    ) -> str:
        return self.staging.put(
            key=key,
            key_components=key_components,
            node=node,
            artifact=artifact,
        )

    def discard(self) -> None:
        self.staging.reset()

    def promote(self) -> int:
        promoted = 0
        for key in self.staging.keys():
            envelope = self.staging.stored_object(key)
            artifact = envelope.get("artifact")
            node = envelope.get("node")
            key_components = envelope.get("key_components")
            if (
                not isinstance(artifact, dict)
                or not isinstance(node, str)
                or not isinstance(key_components, dict)
            ):
                raise corrupt("staged cache object envelope is incomplete", key=key)
            self.committed.put(
                key=key,
                key_components=key_components,
                node=node,
                artifact=artifact,
            )
            promoted += 1
        self.staging.reset()
        return promoted


@dataclass(frozen=True)
class InvalidationPlan:
    affected_request_ids: tuple[str, ...]
    unaffected_request_ids: tuple[str, ...]
    added_request_ids: tuple[str, ...]
    removed_request_ids: tuple[str, ...]
    reused_nodes: tuple[str, ...]
    recomputed_nodes: tuple[str, ...]
    covered_nodes: tuple[str, ...]
    earliest_node_by_request: Mapping[str, str] = field(default_factory=dict)

    def validate(self) -> None:
        reused = set(self.reused_nodes)
        recomputed = set(self.recomputed_nodes)
        covered = set(self.covered_nodes)
        if reused.intersection(recomputed):
            raise corrupt("recompute and reuse node sets intersect")
        if reused.union(recomputed) != covered:
            raise corrupt("recompute/reuse union does not cover affected nodes")
        expected = {
            f"{request_id}:{node}"
            for request_id in self.affected_request_ids
            for node in PIPELINE_NODES
        }
        if covered != expected:
            raise corrupt(
                "affected node coverage is incomplete",
                missing=sorted(expected.difference(covered)),
                extra=sorted(covered.difference(expected)),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "affected_request_ids": list(self.affected_request_ids),
            "unaffected_request_ids": list(self.unaffected_request_ids),
            "added_request_ids": list(self.added_request_ids),
            "removed_request_ids": list(self.removed_request_ids),
            "reused_nodes": list(self.reused_nodes),
            "recomputed_nodes": list(self.recomputed_nodes),
            "covered_nodes": list(self.covered_nodes),
            "earliest_node_by_request": dict(sorted(self.earliest_node_by_request.items())),
            "disjoint": not set(self.reused_nodes).intersection(self.recomputed_nodes),
            "union_covers_all_affected_nodes": (
                set(self.reused_nodes).union(self.recomputed_nodes)
                == set(self.covered_nodes)
            ),
        }


def _request_changed(left: InferenceRequest, right: InferenceRequest) -> bool:
    return left.input_dict() != right.input_dict()


def plan_invalidation(
    previous_requests: Iterable[InferenceRequest],
    current_requests: Iterable[InferenceRequest],
    previous_config: ThresholdConfig,
    current_config: ThresholdConfig,
) -> InvalidationPlan:
    previous_list = tuple(previous_requests)
    current_list = tuple(current_requests)
    previous = {request.request_id: request for request in previous_list}
    current = {request.request_id: request for request in current_list}
    if len(previous) != len(previous_list):
        raise corrupt("duplicate previous request_id")
    if len(current) != len(current_list):
        raise corrupt("duplicate current request_id")

    budget_or_precision_changed = (
        previous_config.route_budget != current_config.route_budget
        or previous_config.precision != current_config.precision
    )
    threshold_changed = (
        previous_config.generation != current_config.generation
        or previous_config.revision != current_config.revision
        or previous_config.threshold_map() != current_config.threshold_map()
        or previous_config.tombstones != current_config.tombstones
        or previous_config.candidate_plan_count != current_config.candidate_plan_count
    )

    affected: list[str] = []
    unaffected: list[str] = []
    earliest: dict[str, str] = {}
    for request_id in sorted(current):
        old = previous.get(request_id)
        if old is None:
            start_index = 1
        elif _request_changed(old, current[request_id]):
            start_index = 1
        elif budget_or_precision_changed:
            start_index = 1
        elif threshold_changed:
            start_index = 3
        else:
            unaffected.append(request_id)
            continue
        affected.append(request_id)
        earliest[request_id] = PIPELINE_NODES[start_index - 1]

    reused: list[str] = []
    recomputed: list[str] = []
    covered: list[str] = []
    for request_id in affected:
        start = NODE_INDEX[earliest[request_id]]
        for index, node in enumerate(PIPELINE_NODES, start=1):
            covered.append(f"{request_id}:{node}")
            if index < start:
                reused.append(f"{request_id}:{node}")
            else:
                recomputed.append(f"{request_id}:{node}")

    plan = InvalidationPlan(
        affected_request_ids=tuple(affected),
        unaffected_request_ids=tuple(unaffected),
        added_request_ids=tuple(sorted(set(current).difference(previous))),
        removed_request_ids=tuple(sorted(set(previous).difference(current))),
        reused_nodes=tuple(reused),
        recomputed_nodes=tuple(recomputed),
        covered_nodes=tuple(covered),
        earliest_node_by_request=earliest,
    )
    plan.validate()
    return plan

