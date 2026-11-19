"""Application service: immutable snapshots, edits, query, cache and checks."""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .aggregate import verify_aggregation
from .cache import (
    JudgmentCache,
    make_cache_key,
)
from .consistency import compare_results
from .dataio import (
    load_inputs,
    validate_config,
    validate_conversations,
    validate_risk_words,
)
from .errors import AppError, fail
from .judge import judge_all, judge_conversation
from .risk import validate_result_risk_hits, validate_risk_span
from .snapshot import (
    Snapshot,
    SnapshotStore,
    build_snapshot,
    validate_snapshot,
)
from .util import clone, message_ids


class CommitGate:
    """Non-blocking single-writer gate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self, stage: str) -> Iterator[None]:
        if not self._lock.acquire(blocking=False):
            fail(
                "E_STAGE_BUSY",
                "another edit or snapshot commit is active",
                stage=stage,
                blocking=False,
            )
        try:
            yield
        finally:
            self._lock.release()

    @contextmanager
    def hold_for_test(self, stage: str = "test") -> Iterator[None]:
        with self.acquire(stage):
            yield


class DomainService:
    def __init__(
        self,
        data_dir: str | Path = "data",
        runtime_dir: str | Path = "runtime",
        *,
        reset: bool = False,
        load_existing: bool = True,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.store = SnapshotStore(self.runtime_dir / "snapshot.json")
        self.commit_gate = CommitGate()
        self._state_lock = threading.RLock()

        inputs = load_inputs(self.data_dir)
        self._baseline_inputs = inputs
        if reset and self.store.path.exists():
            self.store.path.unlink()

        if load_existing and self.store.exists():
            existing = self.store.load()
            baseline_config_version = int(inputs["config"]["config_version"])
            baseline_risk_version = int(inputs["risk_words"]["version"])
            baseline_weights_version = int(inputs["config"]["versions"]["weights"])
            if (
                baseline_config_version > existing.config_version
                or baseline_risk_version > existing.risk_version
                or baseline_weights_version > existing.weights_version
            ):
                snapshot = self._new_baseline_snapshot(
                    inputs,
                    snapshot_version=existing.snapshot_version + 1,
                    version_floor=existing.versions,
                )
                self.store.save(snapshot)
            else:
                snapshot = existing
        else:
            snapshot = self._new_baseline_snapshot(
                inputs,
                snapshot_version=1,
                version_floor=None,
            )
            self.store.save(snapshot)

        self._snapshot = snapshot
        cache_config = snapshot.config["cache"]
        self.cache = JudgmentCache(
            enabled=bool(cache_config["enabled"]),
            max_entries=int(cache_config["max_entries"]),
            resource_limit=int(
                snapshot.config["resource_limits"]["max_cache_entries"]
            ),
        )
        self._prewarm_cache()

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------
    def _new_baseline_snapshot(
        self,
        inputs: dict[str, Any],
        *,
        snapshot_version: int,
        version_floor: dict[str, int] | None,
    ) -> Snapshot:
        conversations = clone(inputs["conversations"])
        config = clone(inputs["config"])
        risk_words = clone(inputs["risk_words"])
        self._validate_conversation_limits(conversations, config)
        results, batch_count = judge_all(
            conversations, config, risk_words, int(config["batch_size"])
        )
        versions = {
            "results": int(config["versions"]["results"]),
            "risk": int(risk_words["version"]),
            "weights": int(config["versions"]["weights"]),
            "config": int(config["config_version"]),
        }
        if version_floor:
            versions["results"] = max(versions["results"], int(version_floor["results"]) + 1)
            versions["risk"] = max(versions["risk"], int(version_floor["risk"]))
            versions["weights"] = max(versions["weights"], int(version_floor["weights"]))
            versions["config"] = max(versions["config"], int(version_floor["config"]))
        config["config_version"] = versions["config"]
        config["versions"]["results"] = versions["results"]
        config["versions"]["weights"] = versions["weights"]
        risk_words["version"] = versions["risk"]
        return build_snapshot(
            snapshot_version=snapshot_version,
            versions=versions,
            conversations=conversations,
            config=config,
            risk_words=risk_words,
            results=results,
            batch_count=batch_count,
        )

    def _prewarm_cache(self) -> None:
        if not self.cache.enabled:
            return
        for conv_id in sorted(self._snapshot.conversations):
            conversation = self._snapshot.conversations[conv_id]
            key = make_cache_key(
                conversation, self._snapshot.config, self._snapshot.risk_words
            )
            self.cache.put(
                key,
                conversation,
                self._snapshot.config,
                self._snapshot.risk_words,
                self._snapshot.snapshot_version,
                self._snapshot.results[conv_id],
            )

    # ------------------------------------------------------------------
    # Resource and state helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_conversation_limits(
        conversations: dict[str, dict[str, Any]], config: dict[str, Any]
    ) -> None:
        limits = config["resource_limits"]
        if len(conversations) > int(limits["max_conversations"]):
            fail(
                "E_RESOURCE_LIMIT",
                "conversation limit exceeded",
                degraded_category="conversations",
                observed=len(conversations),
                limit=int(limits["max_conversations"]),
            )
        total_messages = sum(
            len(conversation["messages"]) for conversation in conversations.values()
        )
        if total_messages > int(limits["max_messages"]):
            fail(
                "E_RESOURCE_LIMIT",
                "message limit exceeded",
                degraded_category="messages",
                observed=total_messages,
                limit=int(limits["max_messages"]),
            )

    def _minimum_versions(self) -> dict[str, int]:
        snap = self._snapshot
        return {
            "snapshot": snap.snapshot_version,
            "results": snap.results_version,
            "risk": snap.risk_version,
            "weights": snap.weights_version,
            "config": snap.config_version,
        }

    @staticmethod
    def _batch_map(
        conversations: dict[str, dict[str, Any]], batch_size: int
    ) -> tuple[dict[str, int], int]:
        keys = sorted(conversations)
        mapping: dict[str, int] = {}
        for offset, conv_id in enumerate(keys):
            mapping[conv_id] = offset // batch_size
        return mapping, (len(keys) + batch_size - 1) // batch_size

    def _commit(
        self,
        *,
        conversations: dict[str, dict[str, Any]],
        config: dict[str, Any],
        risk_words: dict[str, Any],
        versions: dict[str, int],
        content_changed: set[str],
        affected_cache_convs: set[str] | None = None,
        all_entries_changed: bool,
    ) -> dict[str, Any]:
        self._validate_conversation_limits(conversations, config)
        batch_size = int(config["batch_size"])
        batch_map, batch_count = self._batch_map(conversations, batch_size)
        current = self._snapshot

        recompute = set(content_changed) & set(conversations)
        cache_scope = set(content_changed)
        if affected_cache_convs:
            cache_scope.update(affected_cache_convs)
        for conv_id in conversations:
            old = current.results.get(conv_id)
            if old is None or int(old.get("batch_no", -1)) != batch_map[conv_id]:
                recompute.add(conv_id)
                cache_scope.add(conv_id)
        if all_entries_changed:
            recompute = set(conversations)
            cache_scope = set(conversations)

        new_results: dict[str, dict[str, Any]] = {}
        for conv_id in sorted(conversations):
            if conv_id in recompute:
                new_results[conv_id] = judge_conversation(
                    conversations[conv_id], config, risk_words, batch_map[conv_id]
                )
            else:
                result = clone(current.results[conv_id])
                result["batch_no"] = batch_map[conv_id]
                new_results[conv_id] = result

        next_snapshot_version = current.snapshot_version + 1
        snapshot = build_snapshot(
            snapshot_version=next_snapshot_version,
            versions=versions,
            conversations=conversations,
            config=config,
            risk_words=risk_words,
            results=new_results,
            batch_count=batch_count,
        )

        with self._state_lock:
            affected_keys = self.cache.affected_keys(
                conv_ids=cache_scope,
                all_entries=all_entries_changed,
            )
            if all_entries_changed:
                affected_keys = set(self.cache.keys())
            before_entries = len(self.cache)
            next_cache_enabled = bool(config["cache"]["enabled"])
            next_cache_max = min(
                int(config["cache"]["max_entries"]),
                int(config["resource_limits"]["max_cache_entries"]),
            )
            if next_cache_enabled:
                final_entries = (
                    before_entries
                    - len(affected_keys)
                    + len(recompute)
                )
                if final_entries > next_cache_max:
                    fail(
                        "E_RESOURCE_LIMIT",
                        "cache entry limit would be exceeded",
                        degraded_category="cache_entries",
                        observed=final_entries,
                        limit=next_cache_max,
                        stage="cache",
                    )

            self.store.save(snapshot)
            self._snapshot = snapshot
            self.cache.configure(
                enabled=next_cache_enabled,
                max_entries=next_cache_max,
                resource_limit=int(config["resource_limits"]["max_cache_entries"]),
            )
            if self.cache.enabled:
                invalidated = self.cache.invalidate_keys(affected_keys)
                for conv_id in sorted(recompute):
                    conversation = conversations[conv_id]
                    key = make_cache_key(conversation, config, risk_words)
                    self.cache.put(
                        key,
                        conversation,
                        config,
                        risk_words,
                        snapshot.snapshot_version,
                        new_results[conv_id],
                    )
            else:
                invalidated = self.cache.clear()

        return {
            "snapshot_version": snapshot.snapshot_version,
            "versions": clone(snapshot.versions),
            "affected_conversations": sorted(recompute),
            "invalidated_cache_entries": invalidated,
            "preserved_cache_entries": max(0, before_entries - invalidated),
            "cache_entries": len(self.cache),
            "batch_count": batch_count,
        }

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def edit_text(self, conv_id: str, msg_no: int, text: str) -> dict[str, Any]:
        if not isinstance(text, str):
            fail("E_INPUT", "text must be a string", conv_id=conv_id, msg_no=msg_no)
        with self.commit_gate.acquire("edit_text"):
            current = self._snapshot
            if conv_id not in current.conversations:
                fail("E_INPUT", "unknown conversation", conv_id=conv_id)
            conversations = clone(current.conversations)
            conversation = conversations[conv_id]
            message = next(
                (
                    item
                    for item in conversation["messages"]
                    if int(item["msg_no"]) == int(msg_no)
                ),
                None,
            )
            if message is None:
                fail(
                    "E_INPUT",
                    "unknown message",
                    conv_id=conv_id,
                    msg_no=msg_no,
                )
            message["text"] = text
            config = clone(current.config)
            versions = {
                "results": current.results_version + 1,
                "risk": current.risk_version,
                "weights": current.weights_version,
                "config": current.config_version,
            }
            return self._commit(
                conversations=conversations,
                config=config,
                risk_words=clone(current.risk_words),
                versions=versions,
                content_changed={conv_id},
                affected_cache_convs={conv_id},
                all_entries_changed=False,
            )

    def edit_weight(self, conv_id: str, msg_no: int, weight: float) -> dict[str, Any]:
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) <= 0
        ):
            fail(
                "E_INPUT",
                "weight must be a positive number",
                conv_id=conv_id,
                msg_no=msg_no,
                weight=weight,
            )
        with self.commit_gate.acquire("edit_weight"):
            current = self._snapshot
            if conv_id not in current.conversations:
                fail("E_INPUT", "unknown conversation", conv_id=conv_id)
            conversations = clone(current.conversations)
            message = next(
                (
                    item
                    for item in conversations[conv_id]["messages"]
                    if int(item["msg_no"]) == int(msg_no)
                ),
                None,
            )
            if message is None:
                fail("E_INPUT", "unknown message", conv_id=conv_id, msg_no=msg_no)
            message["weight"] = float(weight)
            versions = {
                "results": current.results_version + 1,
                "risk": current.risk_version,
                "weights": current.weights_version + 1,
                "config": current.config_version,
            }
            return self._commit(
                conversations=conversations,
                config=clone(current.config),
                risk_words=clone(current.risk_words),
                versions=versions,
                content_changed={conv_id},
                affected_cache_convs={conv_id},
                all_entries_changed=False,
            )

    def split_conversation(
        self, conv_id: str, at_msg_no: int, new_conv_id: str
    ) -> dict[str, Any]:
        with self.commit_gate.acquire("split_conversation"):
            current = self._snapshot
            if conv_id not in current.conversations:
                fail("E_INPUT", "unknown conversation", conv_id=conv_id)
            if not new_conv_id or new_conv_id in current.conversations:
                fail(
                    "E_INPUT",
                    "new conversation id must be unused and non-empty",
                    new_conv_id=new_conv_id,
                )
            conversations = clone(current.conversations)
            source = conversations[conv_id]
            ordered = sorted(source["messages"], key=lambda item: int(item["msg_no"]))
            if at_msg_no < 2 or at_msg_no > len(ordered):
                fail(
                    "E_INPUT",
                    "split point must leave two non-empty slices",
                    conv_id=conv_id,
                    at_msg_no=at_msg_no,
                    message_count=len(ordered),
                )
            left_messages = ordered[: at_msg_no - 1]
            right_messages = ordered[at_msg_no - 1 :]
            for index, message in enumerate(left_messages, start=1):
                message["conv_id"] = conv_id
                message["msg_no"] = index
            for index, message in enumerate(right_messages, start=1):
                message["conv_id"] = new_conv_id
                message["msg_no"] = index
            source["messages"] = left_messages
            conversations[new_conv_id] = {
                "conv_id": new_conv_id,
                "locale": source["locale"],
                "annotator_group": source["annotator_group"],
                "messages": right_messages,
            }
            validate_conversations(conversations)
            versions = {
                "results": current.results_version + 1,
                "risk": current.risk_version,
                "weights": current.weights_version,
                "config": current.config_version,
            }
            return self._commit(
                conversations=conversations,
                config=clone(current.config),
                risk_words=clone(current.risk_words),
                versions=versions,
                content_changed={conv_id, new_conv_id},
                affected_cache_convs={conv_id, new_conv_id},
                all_entries_changed=False,
            )

    def merge_conversations(
        self,
        left_conv_id: str,
        right_conv_id: str,
        new_conv_id: str | None = None,
    ) -> dict[str, Any]:
        with self.commit_gate.acquire("merge_conversations"):
            current = self._snapshot
            for conv_id in (left_conv_id, right_conv_id):
                if conv_id not in current.conversations:
                    fail("E_INPUT", "unknown conversation", conv_id=conv_id)
            if left_conv_id == right_conv_id:
                fail("E_INPUT", "cannot merge a conversation with itself")
            merged_id = new_conv_id or left_conv_id
            if (
                merged_id not in {left_conv_id, right_conv_id}
                and merged_id in current.conversations
            ):
                fail("E_INPUT", "new conversation id is already in use", new_conv_id=merged_id)
            conversations = clone(current.conversations)
            left = conversations[left_conv_id]
            right = conversations[right_conv_id]
            merged_messages = (
                sorted(left["messages"], key=lambda item: int(item["msg_no"]))
                + sorted(right["messages"], key=lambda item: int(item["msg_no"]))
            )
            for index, message in enumerate(merged_messages, start=1):
                message["conv_id"] = merged_id
                message["msg_no"] = index
            conversations[merged_id] = {
                "conv_id": merged_id,
                "locale": left["locale"],
                "annotator_group": left["annotator_group"],
                "messages": merged_messages,
            }
            if merged_id != left_conv_id:
                conversations.pop(left_conv_id, None)
            conversations.pop(right_conv_id, None)
            validate_conversations(conversations)
            versions = {
                "results": current.results_version + 1,
                "risk": current.risk_version,
                "weights": current.weights_version,
                "config": current.config_version,
            }
            return self._commit(
                conversations=conversations,
                config=clone(current.config),
                risk_words=clone(current.risk_words),
                versions=versions,
                content_changed={merged_id},
                affected_cache_convs={
                    left_conv_id,
                    right_conv_id,
                    merged_id,
                },
                all_entries_changed=False,
            )

    def update_config(self, new_config: dict[str, Any]) -> dict[str, Any]:
        with self.commit_gate.acquire("update_config"):
            current = self._snapshot
            config = clone(new_config)
            config["config_version"] = current.config_version + 1
            config["versions"]["results"] = current.results_version + 1
            config["versions"]["weights"] = current.weights_version
            validate_config(config)
            if set(config["aggregation"]["risk_category_weights"]) != set(
                current.risk_words["categories"]
            ):
                fail(
                    "E_CONFIG",
                    "configured risk categories must match the controlled set",
                    categories=sorted(current.risk_words["categories"]),
                )
            return self._commit(
                conversations=clone(current.conversations),
                config=config,
                risk_words=clone(current.risk_words),
                versions={
                    "results": current.results_version + 1,
                    "risk": current.risk_version,
                    "weights": current.weights_version,
                    "config": current.config_version + 1,
                },
                content_changed=set(current.conversations),
                affected_cache_convs=set(current.conversations),
                all_entries_changed=True,
            )

    def update_risk_words(self, new_risk_words: dict[str, Any]) -> dict[str, Any]:
        with self.commit_gate.acquire("update_risk_words"):
            current = self._snapshot
            risk_words = clone(new_risk_words)
            risk_words["version"] = current.risk_version + 1
            validate_risk_words(risk_words)
            if set(risk_words["categories"]) != set(
                current.config["aggregation"]["risk_category_weights"]
            ):
                fail(
                    "E_CONFIG",
                    "risk categories must match configured category weights",
                    categories=risk_words["categories"],
                )
            return self._commit(
                conversations=clone(current.conversations),
                config=clone(current.config),
                risk_words=risk_words,
                versions={
                    "results": current.results_version + 1,
                    "risk": current.risk_version + 1,
                    "weights": current.weights_version,
                    "config": current.config_version,
                },
                content_changed=set(current.conversations),
                affected_cache_convs=set(current.conversations),
                all_entries_changed=True,
            )

    # ------------------------------------------------------------------
    # Query and batch
    # ------------------------------------------------------------------
    def query(
        self,
        conv_id: str,
        *,
        budget_ms: float | None = None,
        inject_delay_ms: float = 0.0,
    ) -> dict[str, Any]:
        if inject_delay_ms < 0:
            fail("E_INPUT", "inject_delay_ms must be non-negative")
        started_ns = time.perf_counter_ns()
        completed_stages: list[str] = []
        with self._state_lock:
            snapshot = self._snapshot
        budget = (
            float(snapshot.config["query"]["latency_budget_ms"])
            if budget_ms is None
            else float(budget_ms)
        )
        if budget < 0:
            fail("E_INPUT", "latency budget must be non-negative")

        def elapsed_ms() -> float:
            return round((time.perf_counter_ns() - started_ns) / 1_000_000.0, 6)

        def check_budget() -> None:
            observed = elapsed_ms()
            if observed > budget:
                fail(
                    "E_LATENCY_BUDGET",
                    "query exceeded measured latency budget",
                    conv_id=conv_id,
                    observed_ms=observed,
                    budget_ms=budget,
                    completed_stages=list(completed_stages),
                    message_count=len(
                        snapshot.conversations.get(conv_id, {}).get("messages", [])
                    ),
                    risk_word_count=sum(
                        len(entries)
                        for entries in snapshot.risk_words["terms"].values()
                    ),
                    measurement="time.perf_counter_ns local processing loop",
                )

        completed_stages.append("resolve_snapshot")
        check_budget()
        if conv_id not in snapshot.conversations:
            fail("E_INPUT", "unknown conversation", conv_id=conv_id)
        expected = snapshot.results.get(conv_id)
        if expected is None:
            fail(
                "E_INCONSISTENT",
                "snapshot has no batch record for conversation",
                category="batch_record",
                conv_id=conv_id,
                first_diff_msg_no=None,
            )

        conversation = snapshot.conversations[conv_id]
        key = make_cache_key(conversation, snapshot.config, snapshot.risk_words)
        with self._state_lock:
            cache_result = self.cache.get(key)
        completed_stages.append("cache_lookup")
        check_budget()

        cache_hit = cache_result is not None
        if cache_result is None:
            actual = judge_conversation(
                conversation,
                snapshot.config,
                snapshot.risk_words,
                int(expected["batch_no"]),
            )
            completed_stages.append("judge")
        else:
            actual = cache_result
            completed_stages.append("cache_hit")
        if inject_delay_ms:
            time.sleep(float(inject_delay_ms) / 1000.0)
        check_budget()

        compare_results(expected, actual, int(snapshot.config["precision"]["decimals"]))
        completed_stages.append("consistency_check")
        check_budget()

        if not cache_hit:
            with self._state_lock:
                if self._snapshot is snapshot:
                    self.cache.put(
                        key,
                        conversation,
                        snapshot.config,
                        snapshot.risk_words,
                        snapshot.snapshot_version,
                        actual,
                    )
            completed_stages.append("cache_store")
        check_budget()

        envelope = clone(actual)
        observed = elapsed_ms()
        envelope["query_meta"] = {
            "cache_hit": cache_hit,
            "snapshot_version": snapshot.snapshot_version,
            "versions": clone(snapshot.versions),
            "latency": {
                "observed_ms": observed,
                "budget_ms": budget,
                "message_count": len(conversation["messages"]),
                "risk_word_count": sum(
                    len(entries) for entries in snapshot.risk_words["terms"].values()
                ),
                "batch_size": int(snapshot.config["batch_size"]),
                "completed_stages": completed_stages + ["assemble"],
                "measurement": "time.perf_counter_ns around the local query loop",
            },
            "consistency": {
                "ok": True,
                "batch_no": int(expected["batch_no"]),
                "batch_count": snapshot.batch_count,
                "message_set": "match",
            },
            "cache_stats": self.cache.stats_snapshot(),
        }
        check_budget()
        return envelope

    def run_batch(self, *, fresh_recompute: bool = True) -> dict[str, Any]:
        with self._state_lock:
            snapshot = self._snapshot
        if fresh_recompute:
            fresh, batch_count = judge_all(
                snapshot.conversations,
                snapshot.config,
                snapshot.risk_words,
                int(snapshot.config["batch_size"]),
            )
            for conv_id in sorted(fresh):
                compare_results(
                    snapshot.results[conv_id],
                    fresh[conv_id],
                    int(snapshot.config["precision"]["decimals"]),
                )
            if batch_count != snapshot.batch_count:
                fail(
                    "E_INCONSISTENT",
                    "batch count differs from snapshot",
                    category="batch",
                    conv_id="",
                    first_diff_msg_no=None,
                    expected=snapshot.batch_count,
                    actual=batch_count,
                )
            results = fresh
        else:
            results = snapshot.results
        ordered = [clone(results[conv_id]) for conv_id in sorted(results)]
        return {
            "ok": True,
            "snapshot_version": snapshot.snapshot_version,
            "versions": clone(snapshot.versions),
            "batch_count": snapshot.batch_count,
            "batch_size": int(snapshot.config["batch_size"]),
            "conversation_count": len(ordered),
            "message_count": sum(result["message_count"] for result in ordered),
            "risk_word_count": sum(
                len(entries) for entries in snapshot.risk_words["terms"].values()
            ),
            "results": ordered,
        }

    # ------------------------------------------------------------------
    # Self checks and snapshot replacement
    # ------------------------------------------------------------------
    def self_check(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = self._snapshot
            cache_entries = self.cache.entries()
            validate_snapshot(snapshot)
            validate_config(snapshot.config)
            validate_risk_words(snapshot.risk_words)
            if set(snapshot.config["aggregation"]["risk_category_weights"]) != set(
                snapshot.risk_words["categories"]
            ):
                fail(
                    "E_INCONSISTENT",
                    "risk category weights and controlled set differ",
                    category="risk_categories",
                    conv_id="",
                    first_diff_msg_no=None,
                )
            decimals = int(snapshot.config["precision"]["decimals"])
            fresh, batch_count = judge_all(
                snapshot.conversations,
                snapshot.config,
                snapshot.risk_words,
                int(snapshot.config["batch_size"]),
            )
            if batch_count != snapshot.batch_count:
                fail(
                    "E_INCONSISTENT",
                    "self-check batch count mismatch",
                    category="batch",
                    conv_id="",
                    first_diff_msg_no=None,
                )
            for conv_id in sorted(snapshot.conversations):
                conversation = snapshot.conversations[conv_id]
                result = snapshot.results.get(conv_id)
                if result is None:
                    fail(
                        "E_INCONSISTENT",
                        "result is missing for conversation",
                        category="batch_record",
                        conv_id=conv_id,
                        first_diff_msg_no=None,
                    )
                if result.get("message_ids") != message_ids(conversation):
                    fail(
                        "E_INCONSISTENT",
                        "query and batch message sets differ",
                        category="message_set",
                        conv_id=conv_id,
                        first_diff_msg_no=None,
                    )
                validate_result_risk_hits(result, snapshot.risk_words)
                verify_aggregation(result, snapshot.config, snapshot.risk_words)
                compare_results(snapshot.results[conv_id], fresh[conv_id], decimals)

            for entry in cache_entries:
                conversation = snapshot.conversations.get(entry.conv_id)
                if conversation is None:
                    fail(
                        "E_INCONSISTENT",
                        "cache entry cannot be traced to a conversation",
                        category="cache_key",
                        conv_id=entry.conv_id,
                        first_diff_msg_no=None,
                    )
                expected_key = make_cache_key(
                    conversation, snapshot.config, snapshot.risk_words
                )
                if entry.key != expected_key:
                    fail(
                        "E_INCONSISTENT",
                        "cache key no longer matches snapshot inputs",
                        category="cache_key",
                        conv_id=entry.conv_id,
                        first_diff_msg_no=None,
                    )
                expected_versions = {
                    "risk_version": snapshot.risk_version,
                    "config_version": snapshot.config_version,
                }
                observed_versions = {
                    "risk_version": entry.risk_version,
                    "config_version": entry.config_version,
                }
                if observed_versions != expected_versions:
                    fail(
                        "E_INCONSISTENT",
                        "cache entry version cannot be traced to current snapshot",
                        category="cache_snapshot",
                        conv_id=entry.conv_id,
                        first_diff_msg_no=None,
                        expected=expected_versions,
                        actual=observed_versions,
                    )
                if entry.built_snapshot_version > snapshot.snapshot_version:
                    fail(
                        "E_INCONSISTENT",
                        "cache entry was built by a future snapshot",
                        category="cache_snapshot",
                        conv_id=entry.conv_id,
                        first_diff_msg_no=None,
                    )
                try:
                    compare_results(
                        snapshot.results[entry.conv_id], entry.result, decimals
                    )
                except AppError as exc:
                    fail(
                        "E_INCONSISTENT",
                        "cache result differs from batch snapshot",
                        category="cache_result",
                        conv_id=entry.conv_id,
                        first_diff_msg_no=exc.details.get("first_diff_msg_no"),
                        original_category=exc.details.get("category"),
                    )
            if len(cache_entries) > self.cache.max_entries:
                fail(
                    "E_RESOURCE_LIMIT",
                    "cache entries exceed configured limit",
                    degraded_category="cache_entries",
                    observed=len(cache_entries),
                    limit=self.cache.max_entries,
                )
            traceability = {
                "snapshot_version": snapshot.snapshot_version,
                "snapshot_checksum": snapshot.snapshot_checksum,
                "cache_entries": len(cache_entries),
                "risk_version": snapshot.risk_version,
                "weights_version": snapshot.weights_version,
                "config_version": snapshot.config_version,
                "results_version": snapshot.results_version,
            }
        return {
            "ok": True,
            "checks": {
                "snapshot_checksums": "pass",
                "query_batch_results": "pass",
                "query_batch_message_sets": "pass",
                "aggregate_recompute": "pass",
                "risk_spans": "pass",
                "cache_traceability": "pass",
            },
            "traceability": traceability,
            "cache_stats": self.cache.stats_snapshot(),
        }

    def snapshot_info(self) -> dict[str, Any]:
        with self._state_lock:
            info = self._snapshot.info()
            info["cache_stats"] = self.cache.stats_snapshot()
            info["runtime_snapshot_path"] = str(self.store.path)
            return info

    def verify_snapshot_file(
        self, path: str | Path, *, enforce_no_rollback: bool = True
    ) -> dict[str, Any]:
        candidate = SnapshotStore(Path(path)).load(
            minimum_versions=self._minimum_versions() if enforce_no_rollback else None
        )
        return candidate.info()

    def restore_snapshot(self, path: str | Path) -> dict[str, Any]:
        with self.commit_gate.acquire("restore_snapshot"):
            candidate = SnapshotStore(Path(path)).load(
                minimum_versions=self._minimum_versions()
            )
            validate_snapshot(candidate)
            validate_config(candidate.config)
            validate_risk_words(candidate.risk_words)
            if set(candidate.config["aggregation"]["risk_category_weights"]) != set(
                candidate.risk_words["categories"]
            ):
                fail(
                    "E_CONFIG",
                    "snapshot risk categories and weights do not match",
                    categories=sorted(candidate.risk_words["categories"]),
                )
            candidate_enabled = bool(candidate.config["cache"]["enabled"])
            candidate_max = min(
                int(candidate.config["cache"]["max_entries"]),
                int(candidate.config["resource_limits"]["max_cache_entries"]),
            )
            if candidate_enabled and len(candidate.conversations) > candidate_max:
                fail(
                    "E_RESOURCE_LIMIT",
                    "restored snapshot cache policy cannot hold all conversations",
                    degraded_category="cache_entries",
                    observed=len(candidate.conversations),
                    limit=candidate_max,
                )
            with self._state_lock:
                self.store.save(candidate)
                self._snapshot = candidate
                self.cache.configure(
                    enabled=candidate_enabled,
                    max_entries=candidate_max,
                    resource_limit=int(
                        candidate.config["resource_limits"]["max_cache_entries"]
                    ),
                )
                self.cache.clear()
                if self.cache.enabled:
                    self._prewarm_cache()
            return {
                "restored": True,
                "snapshot_version": candidate.snapshot_version,
                "versions": clone(candidate.versions),
            }

    def validate_span(
        self,
        normalized_text: str,
        category: str,
        start: int,
        end: int,
        term: str | None = None,
    ) -> None:
        validate_risk_span(
            normalized_text,
            category,
            start,
            end,
            self._snapshot.risk_words,
            term=term,
        )

    def cache_snapshot(self) -> dict[str, Any]:
        return {
            "stats": self.cache.stats_snapshot(),
            "keys": self.cache.keys(),
        }

