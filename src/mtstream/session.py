"""Interactive chunked decoding session and diagnostics."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Iterable, Sequence

from .cache import PrefixCacheEntry, PrefixCacheStore
from .core import (
    DecodeRangeOutcome,
    EncodedSource,
    decode_range,
    decode_whole,
    encode_source,
    ensure_chunk_consistency,
    estimate_timing_ns,
    first_record_deviation,
    records_semantic_hash,
    semantic_config_checksum,
    source_prefix_encoding_digest,
)
from .errors import (
    E_CHUNK_MISMATCH,
    E_INCONSISTENT,
    E_LATENCY_BUDGET,
    E_RESOURCE_LIMIT,
    E_STAGE_BUSY,
    fail,
)
from .models import (
    BlockPlan,
    BlockResult,
    DecodeConfig,
    DecoderState,
    FullDecodeResult,
    InvalidationReport,
    StepRecord,
    TimingRecord,
    TranslationCase,
)
from .snapshots import SnapshotStore
from .stage import StageCoordinator
from .util import qfloat, sha256_value, stable_hash
from .validation import validate_case, validate_case_for_config, validate_config


class StreamingSession:
    """A deterministic chunked-decoding session.

    Semantic outputs never include timings.  Timings are recorded separately for
    the whole path and the chunk path and are not used by cache keys.
    """

    def __init__(
        self,
        case: TranslationCase,
        config: DecodeConfig | None = None,
    ) -> None:
        self.case = case
        self.config = config or DecodeConfig()
        validate_case_for_config(self.case, self.config)
        self.snapshots = SnapshotStore.from_case(self.case, self.config)
        self.encoded = encode_source(self.case, self.config)
        self.stage = StageCoordinator()
        self._request_lock = threading.RLock()
        self._cache = PrefixCacheStore(self.config.max_cache_entries)
        self._entry_by_boundary: dict[int, PrefixCacheEntry] = {}
        self._active_plan: BlockPlan | None = None
        self._plan_active = False
        self._ready_blocks: dict[int, BlockResult] = {}
        self._next_block_index = 0
        self._full: FullDecodeResult | None = None
        self._timings: dict[str, TimingRecord] = {}
        self._last_invalidation: InvalidationReport | None = None
        self._recent_errors: list[dict[str, Any]] = []
        self._diagnostic_drift: dict[str, Any] | None = None
        self._plan = self._build_plan(self.config.block_size)
        self._current_state = DecoderState.initial(self.config.embedding_dim)
        self._install_root_cache_entry()
        self._resume_limit: int | None = None

    # ------------------------------------------------------------------
    # Planning and resource policy
    # ------------------------------------------------------------------
    def _target_snapshot_fingerprint(self) -> str:
        return stable_hash(
            "target-input-snapshots-v1",
            self.snapshots.get("vocabulary").checksum,
            self.snapshots.get("alignment_table").checksum,
            self.snapshots.get("block_size").checksum,
        )

    def _semantic_config_checksum(self, config: DecodeConfig | None = None) -> str:
        return semantic_config_checksum(config or self.config)

    def _cache_version(self) -> int:
        versions = self.snapshots.versions()
        return max(versions.values(), default=1)

    def _block_memory_per_step(self) -> int:
        # Conservative resident estimate: target score/attention vectors,
        # candidate records and bookkeeping for one step.
        vocab_factor = len(self.case.target_vocab) + 6
        return (
            self.config.embedding_dim * 8 * vocab_factor
            + 2048
        )

    def _build_plan(self, block_size: int) -> BlockPlan:
        candidate_config = self.config.with_changes(block_size=block_size)
        validate_config(candidate_config)
        if len(self.case.source_tokens) > self.config.max_source_tokens:
            fail(
                E_RESOURCE_LIMIT,
                "source sentence exceeds max_source_tokens",
                field="source_tokens",
                data={
                    "length": len(self.case.source_tokens),
                    "limit": self.config.max_source_tokens,
                },
            )
        target_length = self.case.target_length
        if target_length <= 0:
            fail(
                E_RESOURCE_LIMIT,
                "target length is zero",
                field="target_length",
            )
        required_cache_entries = target_length + 1
        if required_cache_entries > self.config.max_cache_entries:
            fail(
                E_RESOURCE_LIMIT,
                "prefix cache entry budget cannot hold the plan",
                field="prefix_cache_entries",
                data={
                    "required": required_cache_entries,
                    "limit": self.config.max_cache_entries,
                },
            )

        requested = int(block_size)
        effective = min(requested, target_length)
        degraded = False
        category: str | None = None
        per_step = self._block_memory_per_step()
        estimated_block_memory = effective * per_step
        if estimated_block_memory > self.config.max_block_memory_bytes:
            memory_limited = max(
                1, self.config.max_block_memory_bytes // max(1, per_step)
            )
            effective = min(effective, memory_limited, target_length)
            degraded = True
            category = "block_memory"
        boundaries = tuple(
            (start, min(start + effective, target_length))
            for start in range(0, target_length, effective)
        )
        versions = self.snapshots.versions()
        checksums = self.snapshots.checksums()
        digest = source_prefix_encoding_digest(
            self.case, self.encoded, target_length
        )
        plan_payload = {
            "case_id": self.case.case_id,
            "source_version": self.case.source_version,
            "align_version": self.case.align_version,
            "source_encoding_digest": digest,
            "block_size_requested": requested,
            "block_size_effective": effective,
            "context_window": self.config.context_window,
            "boundaries": boundaries,
            "snapshot_versions": versions,
            "snapshot_checksums": checksums,
            "degraded": degraded,
            "degradation_category": category,
            "config_checksum": self._semantic_config_checksum(),
        }
        plan_id = sha256_value(plan_payload)
        return BlockPlan(
            plan_id=plan_id,
            case_id=self.case.case_id,
            source_version=self.case.source_version,
            align_version=self.case.align_version,
            source_encoding_digest=digest,
            block_size_requested=requested,
            block_size_effective=effective,
            context_window=self.config.context_window,
            boundaries=boundaries,
            snapshot_versions=versions,
            snapshot_checksums=checksums,
            degraded=degraded,
            degradation_category=category,
            context=self._semantic_config_checksum(),
        )

    @property
    def plan(self) -> BlockPlan:
        return self._plan

    def submit_plan(self, plan: BlockPlan | None = None) -> BlockPlan:
        """Commit a plan without waiting for the stage lock."""

        candidate = plan or self._plan
        if not self.stage.try_acquire("chunk_plan_submit"):
            self.stage.busy_error(attempted_plan=candidate.plan_id)
        try:
            if (
                candidate.case_id != self.case.case_id
                or candidate.source_version != self.case.source_version
                or candidate.align_version != self.case.align_version
            ):
                fail(
                    E_INCONSISTENT,
                    "plan versions do not match the active case",
                    field="plan",
                    data={
                        "block_index": None,
                        "category": "plan_version",
                        "plan_case_id": candidate.case_id,
                    },
                )
            if self._active_plan is not None and self._active_plan != candidate:
                fail(
                    E_STAGE_BUSY,
                    "a different chunk plan already owns the commit stage",
                    field="stage",
                    data={
                        "stage": "chunk_plan_commit",
                        "waited": False,
                        "active_plan_id": self._active_plan.plan_id,
                        "attempted_plan_id": candidate.plan_id,
                    },
                )
            self._active_plan = candidate
            self._plan = candidate
            self._plan_active = True
            return candidate
        finally:
            self.stage.release("chunk_plan_submit")

    def _ensure_plan_active(self) -> None:
        if not self._plan_active:
            self.submit_plan(self._plan)

    # ------------------------------------------------------------------
    # Prefix cache
    # ------------------------------------------------------------------
    def _cache_metadata(
        self,
        boundary_step: int,
        upstream_node_hash: str,
        *,
        block_size: int | None = None,
    ) -> dict[str, Any]:
        return {
            "source_encoding_digest": source_prefix_encoding_digest(
                self.case, self.encoded, boundary_step
            ),
            "align_version": self.case.align_version,
            "align_checksum": self.case.align_checksum,
            "block_size": int(
                block_size
                if block_size is not None
                else self._plan.block_size_effective
            ),
            "context_window": self.config.context_window,
            "upstream_node_hash": upstream_node_hash,
            "boundary_step": int(boundary_step),
            "config_checksum": self._semantic_config_checksum(),
            "snapshot_fingerprint": self._target_snapshot_fingerprint(),
        }

    def _genesis_hash(self) -> str:
        return stable_hash(
            "prefix-cache-genesis-v1", self.case.case_id, self.case.seed
        )

    def _install_root_cache_entry(self) -> None:
        metadata = self._cache_metadata(
            0,
            self._genesis_hash(),
            block_size=self._plan.block_size_effective,
        )
        entry = PrefixCacheStore.make_entry(
            metadata,
            DecoderState.initial(self.config.embedding_dim),
            version=self._cache_version(),
        )
        self._cache.put(entry)
        self._entry_by_boundary[0] = entry

    def _install_block_cache_entries(
        self,
        start_step: int,
        states: Sequence[DecoderState],
        records: Sequence[StepRecord],
    ) -> tuple[str, ...]:
        start_entry = self._entry_by_boundary.get(start_step)
        if start_entry is None:
            fail(
                E_INCONSISTENT,
                "missing prefix cache checkpoint at block start",
                field="cache",
                data={
                    "block_index": start_step // self._plan.block_size_effective,
                    "category": "cache_prefix",
                    "boundary_step": start_step,
                },
            )
        upstream = start_entry.payload_hash
        keys: list[str] = []
        for state, record in zip(states, records):
            metadata = self._cache_metadata(
                state.step,
                upstream,
                block_size=self._plan.block_size_effective,
            )
            entry = PrefixCacheStore.make_entry(
                metadata,
                state,
                version=self._cache_version(),
                record=record,
            )
            stored = self._cache.put(entry)
            self._entry_by_boundary[state.step] = stored
            upstream = stored.payload_hash
            keys.append(stored.key)
        return tuple(keys)

    def _state_at_block_start(
        self, start_step: int
    ) -> DecoderState:
        entry = self._entry_by_boundary.get(start_step)
        if entry is None:
            if self._current_state.step == start_step:
                return self._current_state
            fail(
                E_INCONSISTENT,
                "no reusable prefix cache checkpoint is available",
                field="cache",
                data={
                    "block_index": start_step // self._plan.block_size_effective,
                    "category": "cache_prefix",
                    "boundary_step": start_step,
                },
            )
        verified = self._cache.get(entry.key)
        return verified.state()

    def _current_input_version(self) -> dict[str, Any]:
        return {
            "case_id": self.case.case_id,
            "source_version": self.case.source_version,
            "align_version": self.case.align_version,
            "source_digest": self.case.source_digest,
            "encoding_digest": self.encoded.encoding_digest,
            "config_checksum": self._semantic_config_checksum(),
        }

    def _load_cached_prefix(
        self,
        start_step: int,
        end_step: int,
    ) -> tuple[
        tuple[StepRecord, ...],
        tuple[DecoderState, ...],
        tuple[str, ...],
    ]:
        records: list[StepRecord] = []
        states: list[DecoderState] = []
        keys: list[str] = []
        input_version = self._current_input_version()
        for step in range(start_step, end_step):
            entry = self._entry_by_boundary.get(step + 1)
            if entry is None:
                fail(
                    E_INCONSISTENT,
                    "reusable prefix checkpoint is missing",
                    field="cache",
                    data={
                        "block_index": step // self._plan.block_size_effective,
                        "category": "cache_prefix",
                        "boundary_step": step + 1,
                    },
                )
            verified = self._cache.get(entry.key)
            record_raw = verified.payload.get("record")
            if record_raw is None:
                fail(
                    E_INCONSISTENT,
                    "cached checkpoint has no replay record",
                    field="cache",
                    data={
                        "block_index": step // self._plan.block_size_effective,
                        "category": "cache_record",
                        "boundary_step": step + 1,
                    },
                )
            state = verified.state()
            record = StepRecord.from_dict(record_raw)
            if record.step != step or state.step != step + 1:
                fail(
                    E_INCONSISTENT,
                    "cached prefix position does not match requested boundary",
                    field="cache",
                    data={
                        "block_index": step // self._plan.block_size_effective,
                        "category": "cache_position",
                        "boundary_step": step + 1,
                    },
                )
            records.append(
                replace(
                    record,
                    input_version=dict(input_version),
                    block_index=step
                    // self._plan.block_size_effective,
                )
            )
            states.append(state)
            keys.append(verified.key)
        return tuple(records), tuple(states), tuple(keys)

    # ------------------------------------------------------------------
    # Decode paths
    # ------------------------------------------------------------------
    def full_decode(self) -> FullDecodeResult:
        with self._request_lock:
            if self._full is None:
                self._full = decode_whole(
                    self.case,
                    self.config,
                    self.encoded,
                    cache_entry_count=len(self._cache),
                )
                self._timings["full_decode"] = self._full.timing
            return self._full

    def request_next_block(self) -> BlockResult:
        return self.request_block(self._next_block_index)

    def request_block(self, block_index: int) -> BlockResult:
        with self._request_lock:
            self._ensure_plan_active()
            if block_index in self._ready_blocks:
                return self._ready_blocks[block_index]
            if block_index != self._next_block_index:
                fail(
                    E_INCONSISTENT,
                    "blocks must be requested in order",
                    field="block_index",
                    data={
                        "block_index": block_index,
                        "category": "block_order",
                        "expected_block_index": self._next_block_index,
                    },
                )
            if block_index >= len(self._plan.boundaries):
                fail(
                    E_INCONSISTENT,
                    "block index is outside the active plan",
                    field="block_index",
                    data={
                        "block_index": block_index,
                        "category": "block_range",
                        "block_count": len(self._plan.boundaries),
                    },
                )
            return self._decode_block(block_index)

    def _decode_block(self, block_index: int) -> BlockResult:
        start_step, end_step = self._plan.boundaries[block_index]
        replay_end = start_step
        if self._resume_limit is not None:
            replay_end = max(
                start_step, min(self._resume_limit, end_step)
            )
        prefix_records: tuple[StepRecord, ...] = ()
        prefix_states: tuple[DecoderState, ...] = ()
        prefix_keys: tuple[str, ...] = ()
        cache_read_ns = 0
        if replay_end > start_step:
            cache_started = time.perf_counter_ns()
            (
                prefix_records,
                prefix_states,
                prefix_keys,
            ) = self._load_cached_prefix(start_step, replay_end)
            cache_read_ns = time.perf_counter_ns() - cache_started

        if replay_end == end_step:
            outcome = DecodeRangeOutcome(
                records=prefix_records,
                states=prefix_states,
                state=prefix_states[-1],
                observed_ns=int(cache_read_ns),
                candidate_score_count=0,
            )
            suffix_states: tuple[DecoderState, ...] = ()
            suffix_records: tuple[StepRecord, ...] = ()
            resumed = True
        else:
            decode_start = replay_end
            if replay_end > start_step:
                state_entry = self._entry_by_boundary.get(replay_end)
                if state_entry is None:
                    fail(
                        E_INCONSISTENT,
                        "missing resume checkpoint",
                        field="cache",
                        data={
                            "block_index": block_index,
                            "category": "cache_prefix",
                            "boundary_step": replay_end,
                        },
                    )
                start_state = self._cache.get(state_entry.key).state()
            else:
                start_state = self._state_at_block_start(start_step)
            suffix = decode_range(
                self.case,
                self.config,
                self.encoded,
                start_state,
                decode_start,
                end_step,
                block_size=self._plan.block_size_effective,
            )
            suffix_states = suffix.states
            suffix_records = suffix.records
            outcome = DecodeRangeOutcome(
                records=prefix_records + suffix.records,
                states=prefix_states + suffix.states,
                state=suffix.state,
                observed_ns=int(cache_read_ns) + suffix.observed_ns,
                candidate_score_count=suffix.candidate_score_count,
            )
            resumed = replay_end > start_step
        if self._diagnostic_drift is not None:
            drift = self._diagnostic_drift
            if (
                int(drift["block_index"]) == block_index
                and int(drift["step"]) in range(start_step, end_step)
            ):
                drifted_records = []
                for record in outcome.records:
                    if record.step == int(drift["step"]):
                        field_name = str(drift["field"])
                        delta = float(drift["delta"])
                        if field_name == "confidence":
                            record = replace(
                                record,
                                confidence=qfloat(record.confidence + delta),
                            )
                        elif field_name == "token":
                            record = replace(
                                record, token=str(drift["value"])
                            )
                        elif field_name == "projection":
                            projection = list(record.projection)
                            projection[0] = qfloat(projection[0] + delta)
                            record = replace(record, projection=tuple(projection))
                        else:
                            raise ValueError(
                                f"unsupported diagnostic drift field {field_name}"
                            )
                    drifted_records.append(record)
                outcome = replace(
                    outcome, records=tuple(drifted_records)
                )
        estimated = estimate_timing_ns(
            len(outcome.records),
            outcome.candidate_score_count,
            len(self._cache) + len(outcome.records),
        )
        budget_ns: int | None = None
        within_budget: bool | None = None
        if outcome.candidate_score_count == 0 and replay_end > start_step:
            stage = "chunk_cache_replay"
        elif resumed:
            stage = "chunk_resume_suffix"
        elif block_index == 0:
            stage = "chunk_first_block"
        else:
            stage = "chunk_incremental_block"
        if stage == "chunk_first_block":
            budget_ns = int(round(self.config.latency_budget_ms * 1_000_000))
            within_budget = outcome.observed_ns <= budget_ns
            if not within_budget:
                fail(
                    E_LATENCY_BUDGET,
                    "first visible block exceeded the latency budget",
                    field="latency_budget_ms",
                    data={
                        "block_index": 0,
                        "observed_ns": outcome.observed_ns,
                        "observed_ms": qfloat(
                            outcome.observed_ns / 1_000_000.0
                        ),
                        "estimated_ns": estimated,
                        "estimated_ms": qfloat(estimated / 1_000_000.0),
                        "budget_ns": budget_ns,
                        "completed_stage": "chunk_step_loop",
                        "decoded_steps": len(outcome.records),
                        "candidate_score_count": outcome.candidate_score_count,
                    },
                )
        timing = TimingRecord(
            stage=stage,
            observed_ns=outcome.observed_ns,
            estimated_ns=estimated,
            step_count=len(outcome.records),
            candidate_score_count=outcome.candidate_score_count,
            cache_entry_count=len(self._cache) + len(outcome.records),
            budget_ns=budget_ns,
            within_budget=within_budget,
        )

        full = self.full_decode()
        ensure_chunk_consistency(
            full,
            outcome.records,
            block_index=block_index,
            start_step=start_step,
            precision=self.config.precision,
        )
        new_keys: tuple[str, ...] = ()
        if suffix_records:
            new_keys = self._install_block_cache_entries(
                replay_end, suffix_states, suffix_records
            )
        keys = prefix_keys + new_keys
        tokens = tuple(record.token for record in outcome.records)
        confidences = tuple(record.confidence for record in outcome.records)
        projections = tuple(record.projection for record in outcome.records)
        block = BlockResult(
            block_index=block_index,
            plan_id=self._plan.plan_id,
            start_step=start_step,
            end_step_exclusive=end_step,
            tokens=tokens,
            confidences=confidences,
            projections=projections,
            step_records=outcome.records,
            source_version=self.case.source_version,
            align_version=self.case.align_version,
            cache_entry_keys=keys,
            timing=timing,
            semantic_hash=records_semantic_hash(outcome.records),
            degraded=self._plan.degraded,
            degradation_category=self._plan.degradation_category,
        )
        self._ready_blocks[block_index] = block
        self._current_state = outcome.state
        self._next_block_index = block_index + 1
        if stage not in self._timings:
            self._timings[stage] = timing
        self._timings[f"block_{block_index}"] = timing
        return block

    # ------------------------------------------------------------------
    # Interactive queries
    # ------------------------------------------------------------------
    def get_ready_blocks(
        self, include_candidates: bool = False
    ) -> dict[str, Any]:
        with self._request_lock:
            blocks = [
                self._verified_ready_block(index).to_dict(
                    include_candidates=include_candidates
                )
                for index in sorted(self._ready_blocks)
            ]
            return {
                "case_id": self.case.case_id,
                "plan_id": self._plan.plan_id if self._plan_active else None,
                "ready_block_count": len(blocks),
                "block_count": len(self._plan.boundaries),
                "all_ready": len(blocks) == len(self._plan.boundaries),
                "ready_tokens": [
                    token
                    for _, block in sorted(self._ready_blocks.items())
                    for token in block.tokens
                ],
                "blocks": blocks,
            }

    def query_block(self, block_index: int) -> dict[str, Any]:
        with self._request_lock:
            block = self._verified_ready_block(block_index)
            return block.to_dict(include_candidates=False)

    def query_candidates(self, block_index: int) -> dict[str, Any]:
        with self._request_lock:
            block = self._verified_ready_block(block_index)
            return {
                "block_index": block.block_index,
                "plan_id": block.plan_id,
                "start_step": block.start_step,
                "end_step_exclusive": block.end_step_exclusive,
                "source_version": block.source_version,
                "align_version": block.align_version,
                "steps": [
                    record.to_dict(include_candidates=True)
                    for record in block.step_records
                ],
            }

    def preview_blocks(
        self,
        block_indices: Sequence[int],
        *,
        max_workers: int = 4,
    ) -> dict[str, Any]:
        """Run real, independent block previews concurrently.

        Previews do not submit a plan and do not commit ready blocks.  They
        start from the immutable state stream of the separately timed whole
        decode and are individually compared with the corresponding whole
        records.
        """

        with self._request_lock:
            full = self.full_decode()
            block_size = self._plan.block_size_effective
            prepared: list[tuple[int, int, int, DecoderState]] = []
            for index in sorted(set(int(value) for value in block_indices)):
                if not 0 <= index < len(self._plan.boundaries):
                    fail(
                        E_INCONSISTENT,
                        "preview block index is outside the active plan",
                        field="block_index",
                        data={
                            "block_index": index,
                            "category": "block_range",
                        },
                    )
                start, end = self._plan.boundaries[index]
                state = (
                    DecoderState.initial(self.config.embedding_dim)
                    if start == 0
                    else full.states[start - 1]
                )
                prepared.append((index, start, end, state))

        def run_one(item: tuple[int, int, int, DecoderState]) -> dict[str, Any]:
            index, start, end, state = item
            outcome = decode_range(
                self.case,
                self.config,
                self.encoded,
                state,
                start,
                end,
                block_size=block_size,
            )
            expected = full.step_records[start:end]
            deviation = first_record_deviation(
                expected,
                outcome.records,
                expected_offset=start,
                precision=self.config.precision,
            )
            return {
                "block_index": index,
                "start_step": start,
                "end_step_exclusive": end,
                "tokens": [record.token for record in outcome.records],
                "confidences": [
                    qfloat(record.confidence) for record in outcome.records
                ],
                "observed_ns": outcome.observed_ns,
                "observed_ms": qfloat(outcome.observed_ns / 1_000_000.0),
                "candidate_score_count": outcome.candidate_score_count,
                "consistent": deviation is None,
                "first_deviation": deviation,
            }

        workers = max(1, min(int(max_workers), len(prepared) or 1))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="mtstream-preview"
        ) as executor:
            results = list(executor.map(run_one, prepared))
        inconsistent = [item for item in results if not item["consistent"]]
        if inconsistent:
            first_inconsistent = inconsistent[0]
            deviation = first_inconsistent["first_deviation"]
            fail(
                E_INCONSISTENT,
                "concurrent preview drifted from whole decoding",
                field=deviation["field"],
                data={
                    "block_index": first_inconsistent["block_index"],
                    "category": "preview",
                    "first_deviation_step": deviation["step"],
                },
            )
        return {
            "preview_count": len(results),
            "max_workers": workers,
            "previews": results,
        }

    def _verified_ready_block(self, block_index: int) -> BlockResult:
        block = self._ready_blocks.get(block_index)
        if block is None:
            fail(
                E_INCONSISTENT,
                "requested block is not ready",
                field="block_index",
                data={
                    "block_index": block_index,
                    "category": "block_not_ready",
                },
            )
        if block.source_version != self.case.source_version:
            fail(
                E_INCONSISTENT,
                "block source version differs from the active source version",
                field="source_version",
                data={
                    "block_index": block_index,
                    "category": "source_version",
                    "block_version": block.source_version,
                    "active_version": self.case.source_version,
                },
            )
        if block.align_version != self.case.align_version:
            fail(
                E_INCONSISTENT,
                "block alignment version differs from the active alignment version",
                field="align_version",
                data={
                    "block_index": block_index,
                    "category": "align_version",
                    "block_version": block.align_version,
                    "active_version": self.case.align_version,
                },
            )
        full = self.full_decode()
        if (
            full.source_version != block.source_version
            or full.align_version != block.align_version
        ):
            fail(
                E_INCONSISTENT,
                "whole-decode baseline versions do not match the active block",
                field="versions",
                data={
                    "block_index": block_index,
                    "category": "version",
                },
            )
        expected = full.step_records[
            block.start_step : block.end_step_exclusive
        ]
        deviation = first_record_deviation(
            expected,
            block.step_records,
            expected_offset=block.start_step,
            precision=self.config.precision,
        )
        if deviation is not None:
            category = "token"
            if "confidence" in deviation["field"]:
                category = "confidence"
            elif "projection" in deviation["field"]:
                category = "projection"
            elif "candidate" in deviation["field"]:
                category = "candidates"
            fail(
                E_INCONSISTENT,
                "ready block is inconsistent with whole decoding",
                field=deviation["field"],
                data={
                    "block_index": block_index,
                    "category": category,
                    "first_deviation_step": deviation["step"],
                    "deviation_field": deviation["field"],
                    "expected": deviation["expected"],
                    "actual": deviation["actual"],
                },
            )
        return block

    # ------------------------------------------------------------------
    # Invalidation and local change
    # ------------------------------------------------------------------
    def _changed_source_indices(
        self, new_case: TranslationCase
    ) -> tuple[int, ...]:
        maximum = max(
            len(self.case.source_tokens), len(new_case.source_tokens)
        )
        changed: list[int] = []
        for index in range(maximum):
            before = (
                self.case.source_tokens[index]
                if index < len(self.case.source_tokens)
                else None
            )
            after = (
                new_case.source_tokens[index]
                if index < len(new_case.source_tokens)
                else None
            )
            if before != after:
                changed.append(index)
        return tuple(changed)

    def analyze_invalidation(
        self,
        new_case: TranslationCase | None = None,
        new_config: DecodeConfig | None = None,
    ) -> InvalidationReport:
        new_case = new_case or self.case
        new_config = new_config or self.config
        validate_case(new_case)
        validate_config(new_config)
        changed_positions = set(self._changed_source_indices(new_case))
        source_encoding_reused = (
            new_case.source_tokens == self.case.source_tokens
            and new_case.seed == self.case.seed
            and new_config.embedding_dim == self.config.embedding_dim
        )
        align_structure_changed = (
            new_case.alignments != self.case.alignments
            or new_case.align_version != self.case.align_version
            or new_case.target_vocab != self.case.target_vocab
        )
        identity_changed = (
            new_case.case_id != self.case.case_id
            or new_case.seed != self.case.seed
        )
        config_changed = (
            new_config.block_size != self.config.block_size
            or new_config.context_window != self.config.context_window
            or new_config.embedding_dim != self.config.embedding_dim
            or new_config.precision != self.config.precision
        )
        target_length = self.case.target_length
        reason_parts: list[str] = []
        if changed_positions:
            reason_parts.append("source_tokens")
        if align_structure_changed:
            reason_parts.append("alignment_table")
        if config_changed:
            reason_parts.append("config")
        if identity_changed:
            reason_parts.append("case_identity")
        reason = "+".join(reason_parts) if reason_parts else "no_semantic_change"

        all_invalid = (
            align_structure_changed or config_changed or identity_changed
        )
        invalidated_sources: dict[int, list[int]] = {}
        if not all_invalid and changed_positions:
            for alignment in self.case.alignments:
                if alignment.source_index in changed_positions:
                    invalidated_sources.setdefault(
                        alignment.source_index, []
                    ).append(alignment.target_index)

        if all_invalid:
            first_affected: int | None = 0
        elif invalidated_sources:
            first_affected = min(
                target
                for targets in invalidated_sources.values()
                for target in targets
            )
        else:
            first_affected = None

        if first_affected is None:
            reused_steps = tuple(range(target_length))
            affected_steps: tuple[int, ...] = ()
        else:
            reused_steps = tuple(range(first_affected))
            affected_steps = tuple(range(first_affected, target_length))

        entries = self._cache.entries()
        if all_invalid:
            reused_entries: list[PrefixCacheEntry] = []
            affected_entries = list(entries)
        elif first_affected is None:
            reused_entries = list(entries)
            affected_entries = []
        else:
            reused_entries = [
                entry
                for entry in entries
                if entry.boundary_step <= first_affected
            ]
            affected_entries = [
                entry
                for entry in entries
                if entry.boundary_step > first_affected
            ]
        reused_payload_hashes = {
            str(entry.boundary_step): entry.payload_hash
            for entry in reused_entries
        }
        details = {
            "changed_source_indices": sorted(changed_positions),
            "invalidated_source_dependencies": {
                str(source): sorted(targets)
                for source, targets in sorted(invalidated_sources.items())
            },
            "alignment_changed": align_structure_changed,
            "config_changed": config_changed,
            "identity_changed": identity_changed,
            "reuse_entry_boundary_max": (
                max(
                    (entry.boundary_step for entry in reused_entries),
                    default=None,
                )
            ),
            "reused_payload_hashes": reused_payload_hashes,
            "source_encoding_reused": source_encoding_reused,
            "target_dependency_encoding_reused": (
                first_affected is None or first_affected > 0
            ),
        }
        report = InvalidationReport(
            reason=reason,
            first_affected_step=first_affected,
            affected_steps=affected_steps,
            reused_steps=reused_steps,
            affected_cache_entries=tuple(
                entry.key for entry in affected_entries
            ),
            reused_cache_entries=tuple(entry.key for entry in reused_entries),
            old_source_digest=self.case.source_digest,
            new_source_digest=new_case.source_digest,
            old_align_version=self.case.align_version,
            new_align_version=new_case.align_version,
            old_block_size=self.config.block_size,
            new_block_size=new_config.block_size,
            details=details,
        )
        self._last_invalidation = report
        return report

    def apply_case_change(
        self,
        new_case: TranslationCase,
        new_config: DecodeConfig | None = None,
    ) -> InvalidationReport:
        """Atomically switch inputs while retaining verified cache prefixes."""

        with self._request_lock:
            new_config = new_config or self.config
            report = self.analyze_invalidation(new_case, new_config)
            affected = set(report.affected_cache_entries)
            for key in affected:
                self._cache.delete(key)
            self._entry_by_boundary = {
                boundary: entry
                for boundary, entry in self._entry_by_boundary.items()
                if entry.key not in affected
            }
            old_source_tokens = self.case.source_tokens
            old_seed = self.case.seed
            old_embedding_dim = self.config.embedding_dim
            self.case = new_case
            self.config = new_config
            if (
                self.case.source_tokens != old_source_tokens
                or self.case.seed != old_seed
                or self.config.embedding_dim != old_embedding_dim
            ):
                self.encoded = encode_source(self.case, self.config)
            self.snapshots.update_from_case(self.case, self.config)
            self._plan = self._build_plan(self.config.block_size)
            self._active_plan = None
            self._plan_active = False
            self._full = None
            self._ready_blocks.clear()
            self._next_block_index = 0
            self._timings.clear()
            if 0 in self._entry_by_boundary:
                self._current_state = self._cache.get(
                    self._entry_by_boundary[0].key
                ).state()
            else:
                self._current_state = DecoderState.initial(
                    self.config.embedding_dim
                )
                self._install_root_cache_entry()
            self._resume_limit = (
                None
                if report.first_affected_step == 0
                else (
                    report.first_affected_step
                    if report.first_affected_step is not None
                    else self.case.target_length
                )
            )
            self._last_invalidation = report
            return report

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------
    def record_error(self, error: Any) -> None:
        payload = error.to_dict() if hasattr(error, "to_dict") else {"message": str(error)}
        self._recent_errors.append(payload)
        self._recent_errors = self._recent_errors[-20:]

    def enable_diagnostic_drift(
        self,
        block_index: int,
        step: int,
        *,
        field: str = "confidence",
        delta: float = 0.01,
        value: Any = None,
    ) -> None:
        """Inject a post-step drift so diagnostics can prove detection paths."""

        self._diagnostic_drift = {
            "block_index": int(block_index),
            "step": int(step),
            "field": field,
            "delta": float(delta),
            "value": value,
        }

    def mutate_ready_block_for_diagnostic(
        self,
        block_index: int,
        *,
        field: str = "confidence",
        delta: float = 0.01,
    ) -> None:
        """Mutate an already committed block to exercise query-time checks."""

        with self._request_lock:
            block = self._ready_blocks.get(block_index)
            if block is None:
                fail(
                    E_INCONSISTENT,
                    "requested block is not ready",
                    field="block_index",
                    data={
                        "block_index": block_index,
                        "category": "block_not_ready",
                    },
                )
            records = list(block.step_records)
            first = records[0]
            if field == "confidence":
                first = replace(
                    first, confidence=qfloat(first.confidence + delta)
                )
            elif field == "projection":
                projection = list(first.projection)
                projection[0] = qfloat(projection[0] + delta)
                first = replace(first, projection=tuple(projection))
            elif field == "token":
                first = replace(first, token=f"{first.token}_drift")
            else:
                raise ValueError(f"unsupported diagnostic mutation {field}")
            records[0] = first
            self._ready_blocks[block_index] = replace(
                block, step_records=tuple(records)
            )

    def diagnostics(self) -> dict[str, Any]:
        with self._request_lock:
            first_timing = self._timings.get("chunk_first_block")
            full_timing = self._timings.get("full_decode")
            blocks = [
                {
                    "block_index": block.block_index,
                    "start_step": block.start_step,
                    "end_step_exclusive": block.end_step_exclusive,
                    "tokens": list(block.tokens),
                    "semantic_hash": block.semantic_hash,
                    "observed_ms": block.timing.observed_ms,
                    "estimated_ms": block.timing.estimated_ms,
                    "degraded": block.degraded,
                }
                for _, block in sorted(self._ready_blocks.items())
            ]
            return {
                "case": {
                    "case_id": self.case.case_id,
                    "seed": self.case.seed,
                    "source_version": self.case.source_version,
                    "align_version": self.case.align_version,
                    "source_tokens": list(self.case.source_tokens),
                    "target_length": self.case.target_length,
                    "source_digest": self.case.source_digest,
                },
                "plan": self._plan.to_dict() if self._plan_active else None,
                "planned_plan": self._plan.to_dict(),
                "block_progress": {
                    "ready": len(self._ready_blocks),
                    "total": len(self._plan.boundaries),
                    "ready_tokens": [
                        token
                        for _, block in sorted(self._ready_blocks.items())
                        for token in block.tokens
                    ],
                },
                "blocks": blocks,
                "first_block_observed": (
                    first_timing.to_dict() if first_timing is not None else None
                ),
                "full_decode": (
                    full_timing.to_dict() if full_timing is not None else None
                ),
                "timing_paths_separate": True,
                "block_size": {
                    "requested": self._plan.block_size_requested,
                    "effective": self._plan.block_size_effective,
                    "degraded": self._plan.degraded,
                    "degradation_category": self._plan.degradation_category,
                },
                "block_memory_estimate": {
                    "per_step_bytes": self._block_memory_per_step(),
                    "block_bytes": (
                        self._plan.block_size_effective
                        * self._block_memory_per_step()
                    ),
                    "limit_bytes": self.config.max_block_memory_bytes,
                },
                "snapshots": self.snapshots.to_dict(include_content=False),
                "snapshot_object_ids": self.snapshots.object_ids(),
                "prefix_cache": {
                    "entry_count": len(self._cache),
                    "limit": self.config.max_cache_entries,
                    "boundaries": sorted(self._entry_by_boundary),
                },
                "invalidation": (
                    self._last_invalidation.to_dict()
                    if self._last_invalidation is not None
                    else None
                ),
                "recent_errors": list(self._recent_errors),
                "active_plan": self._plan_active,
                "stage_owner": self.stage.owner,
            }

    def cache_entries(self) -> tuple[PrefixCacheEntry, ...]:
        return self._cache.entries()

    def prefix_cache_store(self) -> PrefixCacheStore:
        return self._cache

