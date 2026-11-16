"""Per-scale pipeline: parse -> recall -> normalize -> fuse -> evaluate.

Every stage is content-addressed. Upstream key changes invalidate only the
affected stage and its downstream consumers; unchanged artifacts are reused
without recomputation. All per-user outputs are keyed by sorted user IDs and
serialized deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import cache as cache_mod
from .channels import Candidate, ChannelEngine
from .config import CHANNEL_NAMES
from .evaluation import evaluate_scale
from .fusion import ChannelResult, FusionEngine
from .parser import ParsedData, parse_all
from .cache import decision_cache_key
from .selector import select_strategy
from .snapshot import SnapshotStore, digest_payload


@dataclass
class UserError:
    user_id: str
    stage: str
    code: str
    exception_type: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "user_id": self.user_id,
            "stage": self.stage,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
        }


@dataclass
class ScaleRunResult:
    scale_id: str
    parsed: ParsedData
    fused: dict[str, dict[str, Any]]
    evaluation: dict[str, Any]
    decision: dict[str, Any]
    channel_stats: dict[str, Any]
    user_errors: list[UserError] = field(default_factory=list)
    ledger: cache_mod.CacheLedger = field(default_factory=cache_mod.CacheLedger)
    cache_keys: dict[str, Any] = field(default_factory=dict)
    shutdown_requested: bool = False


def _serialize_recall(per_user: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {user_id: per_user[user_id] for user_id in sorted(per_user)}


class ScalePipeline:
    def __init__(
        self,
        scale: dict[str, Any],
        config: dict[str, Any],
        cache: cache_mod.ArtifactCache,
        snapshot_store: SnapshotStore,
        shutdown: Any,
    ):
        self.scale = scale
        self.config = config
        self.cache = cache
        self.snapshot_store = snapshot_store
        self.shutdown = shutdown
        self.user_errors: list[UserError] = []
        self.ledger = cache_mod.CacheLedger()
        self.cache_keys: dict[str, Any] = {}

    def run(self) -> ScaleRunResult:
        parsed_key, parsed = self._stage_parse()
        decision = self._stage_decision(parsed_key, parsed)
        recall_keys, recall_artifacts = self._stage_recall(parsed_key, parsed)
        norm_key, norm_stats = self._stage_normalize(recall_keys, recall_artifacts)
        fusion_key, fused = self._stage_fusion(
            parsed_key, norm_key, recall_keys, recall_artifacts, norm_stats, parsed
        )
        evaluation_key, evaluation = self._stage_evaluation(fusion_key, fused, parsed)
        self.cache_keys.update({
            "parsed": parsed_key,
            "recall": recall_keys,
            "norm": norm_key,
            "fusion": fusion_key,
            "evaluation": evaluation_key,
        })
        channel_engine = ChannelEngine(parsed, self.config)
        return ScaleRunResult(
            scale_id=self.scale["scale_id"],
            parsed=parsed,
            fused=fused,
            evaluation=evaluation,
            decision=decision,
            channel_stats=channel_engine.global_stats(),
            user_errors=self.user_errors,
            ledger=self.ledger,
            cache_keys=self.cache_keys,
            shutdown_requested=self.shutdown.should_stop(),
        )

    def _stage_parse(self) -> tuple[str, ParsedData]:
        input_fp = self.scale["input_fingerprint"]
        key = cache_mod.parsed_cache_key(input_fp, self.config)
        cached = self.cache.get(key)
        if cached is not None:
            self.ledger.record("parsed", self.scale["scale_id"], key, True)
            return key, _deserialize_parsed(cached)
        self.ledger.record("parsed", self.scale["scale_id"], key, False)
        parsed = parse_all(
            self.scale["log_path"], self.scale["dictionary_path"],
            list(self.config["interaction_types"]),
        )
        self.cache.put(key, _serialize_parsed(parsed))
        return key, parsed

    def _stage_decision(self, parsed_key: str, parsed: ParsedData) -> dict[str, Any]:
        key = decision_cache_key(parsed_key, int(self.scale["n_users"]))
        cached = self.cache.get(key)
        if cached is not None:
            self.ledger.record("decision", self.scale["scale_id"], key, True)
            return cached
        self.ledger.record("decision", self.scale["scale_id"], key, False)
        decision = select_strategy(parsed, int(self.scale["n_users"]))
        self.cache.put(key, decision)
        return decision

    def _stage_recall(
        self, parsed_key: str, parsed: ParsedData
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        engine = ChannelEngine(parsed, self.config)
        user_ids = sorted(parsed.user_interactions.keys())
        # Cold users (declared but no valid interactions) still get empty results.
        all_declared = [f"user_{idx:04d}" for idx in range(int(self.scale["n_users"]))]
        full_users = sorted(set(user_ids).union(all_declared))
        artifacts: dict[str, dict[str, Any]] = {}
        keys: dict[str, str] = {}
        interval = max(1, int(self.config["runtime"]["snapshot_interval"]))
        for channel in CHANNEL_NAMES:
            key = cache_mod.recall_cache_key(parsed_key, self.config, channel)
            keys[channel] = key
            cached = self.cache.get(key)
            if cached is not None:
                self.ledger.record(f"recall_{channel}", self.scale["scale_id"], key, True)
                artifacts[channel] = cached
                continue
            self.ledger.record(f"recall_{channel}", self.scale["scale_id"], key, False)
            per_user: dict[str, Any] = {}
            for index, user_id in enumerate(full_users, start=1):
                try:
                    candidates, skip_reason = engine.run_channel(channel, user_id)
                    per_user[user_id] = {
                        "candidates": [candidate.to_dict() for candidate in candidates],
                        "skip_reason": skip_reason,
                    }
                except Exception as exc:  # noqa: BLE001 - user-level isolation
                    self._record_user_error(user_id, f"recall_{channel}", exc)
                    per_user[user_id] = {"candidates": [], "skip_reason": "user_exception"}
                if index % interval == 0:
                    self.snapshot_store.save(
                        "recall", index, len(full_users),
                        {"recall_key": key}, digest_payload(per_user),
                    )
                    if self.shutdown.should_stop():
                        break
            payload = _serialize_recall(per_user)
            self.cache.put(key, payload)
            artifacts[channel] = payload
            self.snapshot_store.save(
                "recall", len(full_users), len(full_users),
                {"recall_key": key}, digest_payload(payload),
            )
            if self.shutdown.should_stop():
                break
        return keys, artifacts

    def _stage_normalize(
        self,
        recall_keys: dict[str, str],
        artifacts: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, dict[str, float]]]:
        key = cache_mod.norm_cache_key(recall_keys)
        cached = self.cache.get(key)
        if cached is not None:
            self.ledger.record("normalization", self.scale["scale_id"], key, True)
            return key, cached
        self.ledger.record("normalization", self.scale["scale_id"], key, False)
        stats: dict[str, dict[str, float]] = {}
        for channel in CHANNEL_NAMES:
            scores = [
                float(candidate["score"])
                for per_user in artifacts.get(channel, {}).values()
                for candidate in per_user["candidates"]
            ]
            stats[channel] = {
                "min": round(min(scores), 10) if scores else 0.0,
                "max": round(max(scores), 10) if scores else 0.0,
            }
        self.cache.put(key, stats)
        return key, stats

    def _stage_fusion(
        self,
        parsed_key: str,
        norm_key: str,
        recall_keys: dict[str, str],
        artifacts: dict[str, dict[str, Any]],
        norm_stats: dict[str, dict[str, float]],
        parsed: ParsedData,
    ) -> tuple[str, dict[str, dict[str, Any]]]:
        key = cache_mod.fusion_cache_key(parsed_key, norm_key, recall_keys, self.config)
        cached = self.cache.get(key)
        if cached is not None:
            self.ledger.record("fusion", self.scale["scale_id"], key, True)
            return key, cached
        self.ledger.record("fusion", self.scale["scale_id"], key, False)
        channel_engine = ChannelEngine(parsed, self.config)
        fusion_engine = FusionEngine(parsed, self.config, channel_engine)
        user_ids = sorted({user_id for artifact in artifacts.values() for user_id in artifact})
        fused: dict[str, dict[str, Any]] = {}
        interval = max(1, int(self.config["runtime"]["snapshot_interval"]))
        for index, user_id in enumerate(user_ids, start=1):
            try:
                recall: dict[str, ChannelResult] = {}
                for channel in CHANNEL_NAMES:
                    payload = artifacts[channel][user_id]
                    recall[channel] = ChannelResult(
                        channel,
                        [Candidate.from_dict(row) for row in payload["candidates"]],
                        payload["skip_reason"],
                    )
                fused[user_id] = fusion_engine.fuse(user_id, recall, norm_stats)
            except Exception as exc:  # noqa: BLE001
                self._record_user_error(user_id, "fusion", exc)
                fused[user_id] = _empty_fusion(user_id, self.config)
            if index % interval == 0:
                self.snapshot_store.save(
                    "fusion", index, len(user_ids), {"fusion_key": key},
                    digest_payload(fused),
                )
                if self.shutdown.should_stop():
                    break
        ordered = {user_id: fused[user_id] for user_id in sorted(fused)}
        self.cache.put(key, ordered)
        self.snapshot_store.save(
            "fusion", len(user_ids), len(user_ids), {"fusion_key": key},
            digest_payload(ordered),
        )
        return key, ordered

    def _stage_evaluation(
        self,
        fusion_key: str,
        fused: dict[str, dict[str, Any]],
        parsed: ParsedData,
    ) -> tuple[str, dict[str, Any]]:
        key = cache_mod.evaluation_cache_key(fusion_key, self.config)
        cached = self.cache.get(key)
        if cached is not None:
            self.ledger.record("evaluation", self.scale["scale_id"], key, True)
            return key, cached
        self.ledger.record("evaluation", self.scale["scale_id"], key, False)
        try:
            result = evaluate_scale(parsed, fused, self.config, int(self.scale["n_users"]))
        except Exception as exc:  # noqa: BLE001
            self._record_user_error("*", "evaluation", exc)
            result = {"metrics": {}, "sampled_users": [], "error": str(exc)}
        self.cache.put(key, result)
        self.snapshot_store.save(
            "evaluation", 1, 1, {"evaluation_key": key}, digest_payload(result)
        )
        return key, result

    def _record_user_error(self, user_id: str, stage: str, exc: Exception) -> None:
        from .errors import E_STAGE_USER

        self.user_errors.append(
            UserError(user_id, stage, E_STAGE_USER, type(exc).__name__, str(exc))
        )


def _empty_fusion(user_id: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "strategy": config["fusion"]["strategy"],
        "redistribution": [],
        "active_channels": [],
        "empty_channels": [
            name for name in CHANNEL_NAMES if config["channels"][name]["enabled"]
        ],
        "skipped_channels": {},
        "weights_after_redistribution": {name: 0.0 for name in CHANNEL_NAMES},
        "recommendations": [],
    }


def _serialize_parsed(parsed: ParsedData) -> dict[str, Any]:
    return {
        "interactions": parsed.interactions,
        "item_terms": parsed.item_terms,
        "user_interactions": parsed.user_interactions,
        "user_items": parsed.user_items,
        "item_users": parsed.item_users,
        "user_interaction_counts": parsed.user_interaction_counts,
        "item_interaction_counts": parsed.item_interaction_counts,
        "item_latest_epoch": parsed.item_latest_epoch,
        "time_span": parsed.time_span,
        "failures": parsed.failure_dicts(),
        "user_count": parsed.user_count,
        "item_count": parsed.item_count,
        "interaction_count": parsed.interaction_count,
    }


def _deserialize_parsed(payload: dict[str, Any]) -> ParsedData:
    from .parser import Failure

    data = ParsedData()
    data.interactions = payload["interactions"]
    data.item_terms = payload["item_terms"]
    data.user_interactions = payload["user_interactions"]
    data.user_items = payload["user_items"]
    data.item_users = payload["item_users"]
    data.user_interaction_counts = payload["user_interaction_counts"]
    data.item_interaction_counts = payload["item_interaction_counts"]
    data.item_latest_epoch = payload["item_latest_epoch"]
    data.time_span = payload["time_span"]
    data.failures = [Failure(**row) for row in payload["failures"]]
    data.user_count = payload["user_count"]
    data.item_count = payload["item_count"]
    data.interaction_count = payload["interaction_count"]
    return data
