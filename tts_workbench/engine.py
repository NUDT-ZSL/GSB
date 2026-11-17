from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .commands import (
    CommandRecord,
    RawCommand,
    apply_command,
    edit_digest,
    load_command_files,
    replay_applied_commands,
)
from .config import ENGINE_VERSION, AcousticConfig, load_config
from .errors import ConfigInvalid, SnapshotMismatch
from .features import (
    audio_segment_fingerprint,
    compute_frame_feature,
    concat_cache_key,
    energy_frame_key,
    interval_cache_key,
    pcm_sha256,
    stable_task_key,
    synthesize_energy_normalized_frame,
)
from .inputs import discover_config, load_inputs
from .model import Failure, InputBundle, LoadedUtterance, PhonemeSpec, UtteranceSpec
from .snapshot import SnapshotStore, new_snapshot_state
from .util import atomic_write_json, sha256_json


@dataclass
class RenderResult:
    status: str
    exit_code: int
    report: dict[str, Any]
    report_path: Path


def _round_divide(values: np.ndarray, counts: np.ndarray) -> np.ndarray:
    positive = values >= 0
    result = np.zeros(values.shape, dtype=np.int64)
    safe_counts = np.maximum(counts, 1)
    rounded_positive = (values + safe_counts // 2) // safe_counts
    rounded_negative = -((-values + safe_counts // 2) // safe_counts)
    result[positive] = rounded_positive[positive]
    result[~positive] = rounded_negative[~positive]
    result[counts == 0] = 0
    return result


def _contiguous_ranges(indices: list[int]) -> list[list[int]]:
    if not indices:
        return []
    ranges: list[list[int]] = []
    start = previous = indices[0]
    for value in indices[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append([start, previous + 1])
        start = previous = value
    ranges.append([start, previous + 1])
    return ranges


def _target_db(effective_energy: float, config: AcousticConfig) -> tuple[float, float]:
    clamped = min(1.0, max(0.0, effective_energy))
    if clamped <= 0.0:
        return -120.0, clamped
    return config.energy_target_dbfs + 20.0 * math.log10(clamped), clamped


class Workbench:
    def __init__(
        self,
        *,
        input_dir: Path,
        work_dir: Path,
        stop_event: threading.Event | None = None,
        crash_after_checkpoints: int | None = None,
    ):
        self.input_dir = input_dir.resolve()
        self.work_dir = work_dir.resolve()
        self.stop_event = stop_event or threading.Event()
        self.crash_after_checkpoints = crash_after_checkpoints
        self.checkpoint_count = 0
        self.snapshot_store = SnapshotStore(self.work_dir)
        self.cache_lock = threading.Lock()
        self.cache: dict[str, dict[str, Any]] = {
            "frame_features": {},
            "energy_frames": {},
            "concat_pcm": {},
        }
        self.inputs: InputBundle | None = None
        self.specs: dict[str, UtteranceSpec] = {}
        self.command_records: list[CommandRecord] = []
        self.failures: list[Failure] = []
        self.recovery_point: dict[str, Any] | None = None
        self.resumed_snapshot_stage: str | None = None

    def run(self, *, extra_command_paths: list[Path] | None = None, reset: bool = False) -> RenderResult:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        extra_command_paths = extra_command_paths or []
        preflight_snapshot: dict[str, Any] | None = None
        if not reset:
            preflight_config: AcousticConfig | None = None
            try:
                preflight_config = load_config(discover_config(self.input_dir))
            except ConfigInvalid:
                preflight_config = None
            if preflight_config is not None:
                try:
                    preflight_snapshot = self.snapshot_store.load()
                    if preflight_snapshot is not None:
                        self.snapshot_store.validate(
                            preflight_snapshot,
                            config_hash=preflight_config.config_hash,
                        )
                except SnapshotMismatch as exc:
                    return self._rejection_report(
                        exc,
                        snapshot=preflight_snapshot,
                        config=preflight_config,
                    )
        try:
            self.inputs = load_inputs(self.input_dir)
        except ConfigInvalid as exc:
            return self._early_failure(
                code=exc.code,
                message=exc.message,
                details=exc.details,
                report_name="report.json",
                exit_code=2,
            )

        self.failures = list(self.inputs.failures)
        if not self.inputs.utterances:
            report = self._base_report(status="failed", config=self.inputs.config)
            report["failures"] = [failure.to_dict() for failure in self.failures]
            report["business_codes"] = sorted({failure.code for failure in self.failures})
            report["business_code_summary"] = self._business_code_summary(
                self.failures, []
            )
            path = self.work_dir / "report.json"
            atomic_write_json(path, report)
            return RenderResult("failed", 2, report, path)

        config = self.inputs.config
        self.work_dir.mkdir(parents=True, exist_ok=True)
        snapshot = preflight_snapshot

        state = new_snapshot_state(
            config_hash=config.config_hash,
            label_hash=self.inputs.label_hash,
            stage="load_validate",
        )
        cache_reset_reason: str | None = None
        if snapshot is not None:
            if snapshot.get("label_hash") != self.inputs.label_hash:
                cache_reset_reason = "label_hash_changed"
            else:
                state = snapshot
                self.cache = self._normalise_cache(snapshot.get("cache", {}))
                self.resumed_snapshot_stage = str(snapshot.get("stage", "unknown"))
                self.recovery_point = {
                    "source": "snapshot",
                    "snapshot_path": str(self.snapshot_store.path),
                    "stage": snapshot.get("stage"),
                    "command_cursor": snapshot.get("command_cursor", -1),
                    "completed_phonemes": list(snapshot.get("completed_phonemes", [])),
                    "active_phoneme": snapshot.get("active_phoneme"),
                    "next_substate": snapshot.get("recovery", {}).get(
                        "next_substate", "VOICING_DECISION"
                    ),
                }
        if reset:
            cache_reset_reason = "explicit_reset"
        if cache_reset_reason is not None and self.recovery_point is None:
            self.recovery_point = {
                "source": "full",
                "stage": None,
                "reason": cache_reset_reason,
                "next_substate": "VOICING_DECISION",
            }

        state["config_hash"] = config.config_hash
        state["label_hash"] = self.inputs.label_hash
        state["failures"] = [failure.to_dict() for failure in self.failures]

        command_paths = list(self.inputs.commands_paths) + [Path(path).resolve() for path in extra_command_paths]
        raw_commands, command_parse_failures = load_command_files(command_paths)
        self.failures.extend(command_parse_failures)

        persistent_records = state.get("command_records", [])
        if not isinstance(persistent_records, list):
            persistent_records = []
        try:
            self.specs, replayed = replay_applied_commands(
                self.inputs.utterances, persistent_records, config
            )
        except ValueError as exc:
            report = self._base_report(status="failed", config=config)
            report["failures"] = [failure.to_dict() for failure in self.failures]
            report["business_codes"] = ["E_CONFIG_MISMATCH"]
            report["business_code_summary"] = [
                {"code": "E_CONFIG_MISMATCH", "count": 1, "scopes": ["snapshot"]}
            ]
            report["message"] = f"snapshot command replay failed: {exc}"
            path = self.work_dir / "report.rejected.json"
            atomic_write_json(path, report)
            return RenderResult("rejected", 78, report, path)

        self.command_records = [self._record_from_dict(item) for item in persistent_records if item.get("status") == "applied"]
        # Keep rejected command records as part of the deterministic command
        # history too, so a later restart cannot unexpectedly retry them.
        persisted_all = [self._record_from_dict(item) for item in persistent_records]
        known_seqs = {record.seq for record in persisted_all}
        cursor = max(known_seqs, default=-1)
        duplicate_records: list[CommandRecord] = []

        for raw in raw_commands:
            if raw.seq in known_seqs:
                duplicate_records.append(
                    CommandRecord(
                        seq=raw.seq,
                        op=str(raw.payload.get("op", "")),
                        utterance_id=(
                            raw.payload.get("utterance_id")
                            if isinstance(raw.payload.get("utterance_id"), str)
                            else None
                        ),
                        phoneme_index=(
                            raw.payload.get("phoneme_index")
                            if isinstance(raw.payload.get("phoneme_index"), int)
                            and not isinstance(raw.payload.get("phoneme_index"), bool)
                            else None
                        ),
                        status="skipped_duplicate",
                        message="sequence number was already processed",
                        payload=raw.payload,
                        path=str(raw.path),
                        line=raw.line,
                    )
                )
                continue
            if raw.seq <= cursor:
                record = CommandRecord(
                    seq=raw.seq,
                    op=str(raw.payload.get("op", "")),
                    utterance_id=(
                        raw.payload.get("utterance_id")
                        if isinstance(raw.payload.get("utterance_id"), str)
                        else None
                    ),
                    phoneme_index=None,
                    status="rejected",
                    code="E_COMMAND_ORDER",
                    message=f"seq {raw.seq} is older than cursor {cursor}",
                    payload=raw.payload,
                    path=str(raw.path),
                    line=raw.line,
                )
            else:
                record = apply_command(self.specs, raw, config)
            persisted_all.append(record)
            known_seqs.add(raw.seq)
            cursor = max(cursor, raw.seq)

        for record in persisted_all:
            if record.status == "rejected" and record.code:
                self.failures.append(
                    Failure(
                        code=record.code,
                        scope="command",
                        message=record.message or "command rejected",
                        utterance_id=record.utterance_id,
                        path=record.path,
                        line=record.line,
                        details={"seq": record.seq, "op": record.op},
                    )
                )
        self.failures.sort(
            key=lambda item: (
                item.scope,
                item.path or "",
                item.line if item.line is not None else -1,
                item.utterance_id or "",
                item.code,
                (item.details.get("seq", -1),),
            )
        )

        state["command_records"] = [record.to_dict() for record in persisted_all]
        state["command_cursor"] = cursor
        state["failures"] = [failure.to_dict() for failure in self.failures]
        self.command_records = persisted_all + duplicate_records

        self._checkpoint(
            state,
            stage="load_validate",
            completed=[],
            active=None,
            substate_positions={},
            partial_summary={},
        )
        if self.stop_event.is_set():
            report = self._base_report(status="interrupted", config=config)
            report["recovery_point"] = self._interruption_recovery("load_validate", "VOICING_DECISION")
            return self._finish_report(report, status="interrupted", exit_code=0)

        tasks: list[tuple[tuple[str, int, int], LoadedUtterance, PhonemeSpec, int]] = []
        for utterance in sorted(self.inputs.utterances, key=lambda item: item.utterance_id):
            spec = self.specs[utterance.utterance_id]
            spec.refresh_frame_starts()
            for phoneme in spec.phonemes:
                start = spec.frame_start_by_phoneme[phoneme.index]
                tasks.append(
                    (stable_task_key(utterance.utterance_id, phoneme.index, start), utterance, phoneme, start)
                )
        tasks.sort(key=lambda item: item[0])

        completed_phonemes: list[str] = []
        phoneme_results: dict[str, dict[str, Any]] = {}
        feature_cache_hits = 0
        feature_cache_misses = 0
        energy_cache_hits = 0
        energy_cache_misses = 0
        frames_since_checkpoint = 0
        batch_size = max(1, config.concurrency)

        with ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="tts-phoneme") as pool:
            for batch_start in range(0, len(tasks), batch_size):
                if self.stop_event.is_set():
                    break
                batch = tasks[batch_start : batch_start + batch_size]
                futures = [
                    pool.submit(self._render_phoneme_task, utterance, phoneme, frame_start)
                    for _, utterance, phoneme, frame_start in batch
                ]
                batch_results = [future.result() for future in futures]
                for result in sorted(batch_results, key=lambda item: item["stable_key"]):
                    phoneme_results[self._phoneme_identity(result)] = result
                    completed_phonemes.append(self._phoneme_identity(result))
                    feature_cache_hits += result["cache"]["feature_hits"]
                    feature_cache_misses += result["cache"]["feature_misses"]
                    energy_cache_hits += result["cache"]["energy_hits"]
                    energy_cache_misses += result["cache"]["energy_misses"]
                    frames_since_checkpoint += result["frame_count"]
                completed_phonemes.sort()
                needs_checkpoint = (
                    frames_since_checkpoint >= config.snapshot_interval_frames
                    or self.stop_event.is_set()
                )
                if needs_checkpoint:
                    active = None
                    substate = "VOICING_DECISION"
                    if batch_start + batch_size < len(tasks):
                        active = self._task_identity(tasks[batch_start + batch_size])
                    elif self.stop_event.is_set():
                        active = None
                    self._checkpoint(
                        state,
                        stage="phoneme_features",
                        completed=completed_phonemes,
                        active=active,
                        substate_positions={
                            key: "DONE" for key in completed_phonemes
                        },
                        partial_summary=self._partial_feature_summary(phoneme_results),
                    )
                    frames_since_checkpoint = 0
                if self.stop_event.is_set():
                    break

        if self.stop_event.is_set():
            self._checkpoint(
                state,
                stage="phoneme_features",
                completed=completed_phonemes,
                active=None,
                substate_positions={key: "DONE" for key in completed_phonemes},
                partial_summary=self._partial_feature_summary(phoneme_results),
            )
            report = self._base_report(status="interrupted", config=config)
            report["recovery_point"] = self._interruption_recovery(
                "phoneme_features", "VOICING_DECISION"
            )
            return self._finish_report(report, status="interrupted", exit_code=0)

        if len(completed_phonemes) < len(tasks):
            self._checkpoint(
                state,
                stage="phoneme_features",
                completed=completed_phonemes,
                active=None,
                substate_positions={key: "DONE" for key in completed_phonemes},
                partial_summary=self._partial_feature_summary(phoneme_results),
            )
            report = self._base_report(status="interrupted", config=config)
            report["recovery_point"] = self._interruption_recovery(
                "phoneme_features", "VOICING_DECISION"
            )
            return self._finish_report(report, status="interrupted", exit_code=0)

        self._checkpoint(
            state,
            stage="frame_concat",
            completed=completed_phonemes,
            active=None,
            substate_positions={key: "DONE" for key in completed_phonemes},
            partial_summary=self._partial_feature_summary(phoneme_results),
        )

        concat_results: dict[str, dict[str, Any]] = {}
        concat_hits = 0
        concat_misses = 0
        utterance_specs = sorted(self.specs.values(), key=lambda item: item.utterance_id)
        with ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="tts-concat") as pool:
            for batch_start in range(0, len(utterance_specs), batch_size):
                if self.stop_event.is_set():
                    break
                batch = utterance_specs[batch_start : batch_start + batch_size]
                futures = [
                    pool.submit(
                        self._concat_utterance_task,
                        spec,
                        [
                            phoneme_results[self._phoneme_identity_from_spec(spec, phoneme)]
                            for phoneme in spec.phonemes
                        ],
                    )
                    for spec in batch
                ]
                for result in sorted(
                    (future.result() for future in futures), key=lambda item: item["stable_key"]
                ):
                    concat_results[result["utterance_id"]] = result
                    if result["concat_hit"]:
                        concat_hits += 1
                    else:
                        concat_misses += 1
                if self.stop_event.is_set():
                    break

        if self.stop_event.is_set():
            self._checkpoint(
                state,
                stage="frame_concat",
                completed=completed_phonemes,
                active=None,
                substate_positions={key: "DONE" for key in completed_phonemes},
                partial_summary=self._partial_concat_summary(concat_results),
            )
            report = self._base_report(status="interrupted", config=config)
            report["recovery_point"] = self._interruption_recovery(
                "frame_concat", "FRAME_CONCAT"
            )
            return self._finish_report(report, status="interrupted", exit_code=0)

        self._checkpoint(
            state,
            stage="writeback",
            completed=completed_phonemes,
            active=None,
            substate_positions={key: "DONE" for key in completed_phonemes},
            partial_summary=self._partial_concat_summary(concat_results),
        )
        output_dir = self.work_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: dict[str, dict[str, Any]] = {}
        for utterance_id in sorted(concat_results):
            if self.stop_event.is_set():
                self._checkpoint(
                    state,
                    stage="writeback",
                    completed=completed_phonemes,
                    active=None,
                    substate_positions={key: "DONE" for key in completed_phonemes},
                    partial_summary=self._partial_concat_summary(concat_results),
                )
                report = self._base_report(status="interrupted", config=config)
                report["recovery_point"] = self._interruption_recovery(
                    "writeback", "WRITE"
                )
                return self._finish_report(report, status="interrupted", exit_code=0)
            output = self._write_utterance_output(
                config=config,
                concat_result=concat_results[utterance_id],
                output_dir=output_dir,
            )
            outputs[utterance_id] = output

        report = self._build_complete_report(
            config=config,
            phoneme_results=phoneme_results,
            concat_results=concat_results,
            outputs=outputs,
            completed_phonemes=completed_phonemes,
            cache_stats={
                "feature_hits": feature_cache_hits,
                "feature_misses": feature_cache_misses,
                "energy_hits": energy_cache_hits,
                "energy_misses": energy_cache_misses,
                "concat_hits": concat_hits,
                "concat_misses": concat_misses,
            },
        )
        self._checkpoint(
            state,
            stage="complete",
            completed=completed_phonemes,
            active=None,
            substate_positions={key: "DONE" for key in completed_phonemes},
            partial_summary=self._partial_concat_summary(concat_results),
        )
        path = self._write_report(report, name="report.json")
        return RenderResult("complete", 0, report, path)

    def _render_phoneme_task(
        self,
        utterance: LoadedUtterance,
        phoneme: PhonemeSpec,
        frame_start: int,
    ) -> dict[str, Any]:
        config = self.inputs.config  # type: ignore[union-attr]
        f0_digest = edit_digest(phoneme.f0_edit_seqs)
        energy_digest = edit_digest(phoneme.energy_edit_seqs)
        voicing_digest = edit_digest(phoneme.voicing_edit_seqs)
        frame_rows: list[dict[str, Any]] = []
        frame_keys: list[str] = []
        energy_keys: list[str] = []
        feature_hits = 0
        feature_misses = 0
        energy_hits = 0
        energy_misses = 0

        for local_index, frame in enumerate(phoneme.frames):
            global_frame = frame_start + local_index
            global_sample = global_frame * config.frame_shift_samples
            audio_fingerprint = audio_segment_fingerprint(
                utterance.audio, global_sample, config.frame_length_samples
            )
            feature, feature_key = compute_frame_feature(
                frame=frame,
                f0_scale=phoneme.f0_scale,
                config=config,
                audio_fingerprint=audio_fingerprint,
                f0_edit_digest=f0_digest,
                utterance_id=utterance.utterance_id,
                phone=phoneme.phone,
                global_frame_start=global_frame,
            )
            with self.cache_lock:
                cached_feature = self.cache["frame_features"].get(feature_key)
                if cached_feature is None:
                    self.cache["frame_features"][feature_key] = feature
                    feature_hit = False
                    feature_misses += 1
                else:
                    feature = cached_feature
                    feature_hit = True
                    feature_hits += 1

            effective_energy = frame.energy * phoneme.energy_scale
            target_dbfs, clamped_energy = _target_db(effective_energy, config)
            energy_key = energy_frame_key(
                feature_key=feature_key,
                effective_energy=clamped_energy,
                target_dbfs=target_dbfs,
                global_frame_start=global_frame,
                config=config,
                energy_edit_digest=energy_digest,
            )
            with self.cache_lock:
                cached_energy = self.cache["energy_frames"].get(energy_key)
                if cached_energy is None:
                    energy_hit = False
                    energy_misses += 1
                else:
                    energy_hit = True
                    energy_hits += 1
            if cached_energy is None:
                energy_result = synthesize_energy_normalized_frame(
                    feature=feature,
                    effective_energy=clamped_energy,
                    config=config,
                    global_frame_start=global_frame,
                )
                cache_entry = {
                    key: value for key, value in energy_result.items() if key != "pcm"
                }
                cache_entry["pcm"] = [
                    int(value) for value in np.asarray(energy_result["pcm"]).tolist()
                ]
                with self.cache_lock:
                    self.cache["energy_frames"].setdefault(energy_key, cache_entry)
                    cached_energy = self.cache["energy_frames"][energy_key]
            pcm = np.asarray(cached_energy["pcm"], dtype=np.int16)
            frame_rows.append(
                {
                    "local_index": local_index,
                    "global_frame": global_frame,
                    "feature": feature,
                    "feature_key": feature_key,
                    "energy_key": energy_key,
                    "feature_hit": feature_hit,
                    "energy_hit": energy_hit,
                    "energy": cached_energy,
                    "pcm": pcm,
                }
            )
            frame_keys.append(feature_key)
            energy_keys.append(energy_key)

        invalid_indices = [
            row["local_index"]
            for row in frame_rows
            if not (row["feature_hit"] and row["energy_hit"])
        ]
        feature_miss_indices = [row["local_index"] for row in frame_rows if not row["feature_hit"]]
        energy_miss_indices = [row["local_index"] for row in frame_rows if not row["energy_hit"]]
        reused_indices = [
            row["local_index"]
            for row in frame_rows
            if row["feature_hit"] and row["energy_hit"]
        ]
        invalidated_states: list[str] = []
        if feature_miss_indices:
            if phoneme.voicing_edit_seqs:
                invalidated_states.append("VOICING_DECISION")
            invalidated_states.extend(["F0_TRACK", "SPECTRAL_ENVELOPE"])
        if feature_miss_indices or energy_miss_indices:
            invalidated_states.extend(["ENERGY_NORMALIZE", "FRAME_CONCAT"])
        invalidated_states = list(dict.fromkeys(invalidated_states))

        intervals: list[dict[str, Any]] = []
        for start in range(0, len(frame_rows), config.max_batch_frames):
            rows = frame_rows[start : start + config.max_batch_frames]
            keys = frame_keys[start : start + config.max_batch_frames]
            ekeys = energy_keys[start : start + config.max_batch_frames]
            intervals.append(
                {
                    "start_frame": start,
                    "end_frame": start + len(rows),
                    "start_global_frame": rows[0]["global_frame"],
                    "end_global_frame": rows[-1]["global_frame"] + 1,
                    "cache_key": interval_cache_key(keys, ekeys, config),
                    "hit_frames": sum(
                        1 for row in rows if row["feature_hit"] and row["energy_hit"]
                    ),
                    "miss_frames": sum(
                        1 for row in rows if not (row["feature_hit"] and row["energy_hit"])
                    ),
                }
            )

        state_trace = self._build_state_trace(phoneme, frame_rows, config)
        phoneme_key = sha256_json(
            {
                "acoustic_hash": config.acoustic_hash,
                "utterance_id": utterance.utterance_id,
                "audio_hash": utterance.audio_hash,
                "label_hash": utterance.label_hash,
                "phoneme_index": phoneme.index,
                "phone": phoneme.phone,
                "frame_start": frame_start,
                "frame_count": len(frame_rows),
                "frame_keys": frame_keys,
                "energy_keys": energy_keys,
                "f0_edit_digest": f0_digest,
                "energy_edit_digest": energy_digest,
                "voicing_edit_digest": voicing_digest,
            }
        )
        return {
            "stable_key": stable_task_key(utterance.utterance_id, phoneme.index, frame_start),
            "utterance_id": utterance.utterance_id,
            "phoneme_index": phoneme.index,
            "phone": phoneme.phone,
            "frame_start": frame_start,
            "frame_count": len(frame_rows),
            "phoneme_key": phoneme_key,
            "frame_keys": frame_keys,
            "energy_keys": energy_keys,
            "intervals": intervals,
            "frames": frame_rows,
            "state_trace": state_trace,
            "substate_position": "DONE",
            "cache": {
                "feature_hits": feature_hits,
                "feature_misses": feature_misses,
                "energy_hits": energy_hits,
                "energy_misses": energy_misses,
                "reused": not invalid_indices,
            },
            "reuse_ranges": _contiguous_ranges(reused_indices),
            "invalidation_ranges": _contiguous_ranges(invalid_indices),
            "invalidated_states": invalidated_states,
            "f0_hz": [float(row["feature"]["f0_hz"]) for row in frame_rows],
            "voiced": [bool(row["feature"]["voiced"]) for row in frame_rows],
            "spectral_bands": [row["feature"]["spectral_bands"] for row in frame_rows],
            "frame_energy_db": [
                self._pcm_db(row["pcm"]) for row in frame_rows
            ],
            "edit_digests": {
                "f0": f0_digest,
                "energy": energy_digest,
                "voicing": voicing_digest,
            },
        }

    def _build_state_trace(
        self,
        phoneme: PhonemeSpec,
        rows: list[dict[str, Any]],
        config: AcousticConfig,
    ) -> list[dict[str, Any]]:
        voicing_comparisons = []
        for row in rows:
            decision = row["feature"]["voicing_decision"]
            voicing_comparisons.append(
                {
                    "frame_index": row["local_index"],
                    "voicing_score": decision["score"],
                    "threshold": decision["threshold"],
                    "operator": decision["operator"],
                    "result": decision["score_result"],
                    "final_voiced": row["feature"]["voiced"],
                    "source": decision["source"],
                    "tie_rule": decision["tie_rule"],
                }
            )
        f0_values = [float(row["feature"]["f0_hz"]) for row in rows]
        raw_f0_values = [float(row["feature"]["f0_raw_hz"]) for row in rows]
        band_sum = [float(sum(row["feature"]["spectral_bands"])) for row in rows]
        gains = [int(row["energy"]["gain_q16"]) for row in rows]
        raw_rms = [int(row["energy"]["raw_rms_q15"]) for row in rows]
        target_rms = [int(row["energy"]["target_rms_q15"]) for row in rows]
        clipped = sum(int(row["energy"]["clipped_samples"]) for row in rows)
        return [
            {
                "state": "VOICING_DECISION",
                "status": "done",
                "transition": {
                    "condition": "voicing_score >= voicing_threshold",
                    "tie_rule": "equality resolves to voiced",
                    "evaluated_frames": len(rows),
                    "true_count": sum(1 for item in voicing_comparisons if item["result"]),
                    "false_count": sum(1 for item in voicing_comparisons if not item["result"]),
                    "forced_unvoiced_by_zero_f0": sum(
                        1 for row in rows if row["feature"]["forced_unvoiced_reason"]
                    ),
                },
                "features": {"comparisons": voicing_comparisons},
            },
            {
                "state": "F0_TRACK",
                "status": "done",
                "transition": {
                    "condition": "quantized f0 is clamped into the configured search range",
                    "f0_min_hz": config.f0_min_hz,
                    "f0_max_hz": config.f0_max_hz,
                    "quantization_hz": config.f0_quantization_hz,
                    "clamped_low_count": sum(
                        1 for row in rows if row["feature"]["f0_clamped_low"]
                    ),
                    "clamped_high_count": sum(
                        1 for row in rows if row["feature"]["f0_clamped_high"]
                    ),
                },
                "features": {
                    "raw_f0_min_hz": min(raw_f0_values) if raw_f0_values else None,
                    "raw_f0_max_hz": max(raw_f0_values) if raw_f0_values else None,
                    "quantized_f0_min_hz": min(f0_values) if f0_values else None,
                    "quantized_f0_max_hz": max(f0_values) if f0_values else None,
                    "quantized_f0": f0_values,
                    "f0_sha256": sha256_json(f0_values),
                },
            },
            {
                "state": "SPECTRAL_ENVELOPE",
                "status": "done",
                "transition": {
                    "condition": "harmonic frequency <= Nyquist frequency",
                    "nyquist_hz": config.sample_rate / 2.0,
                    "band_count": config.spectral_bands,
                    "frames_with_all_bands": sum(
                        1
                        for row in rows
                        if len(row["feature"]["active_harmonics"]) == config.spectral_bands
                    ),
                },
                "features": {
                    "band_energy_min": min(band_sum) if band_sum else None,
                    "band_energy_max": max(band_sum) if band_sum else None,
                    "band_energy_mean": (sum(band_sum) / len(band_sum)) if band_sum else None,
                    "band_envelopes_sha256": sha256_json(
                        [row["feature"]["spectral_bands"] for row in rows]
                    ),
                },
            },
            {
                "state": "ENERGY_NORMALIZE",
                "status": "done",
                "transition": {
                    "condition": "fixed Q16 gain maps raw RMS to target RMS",
                    "target_dbfs": config.energy_target_dbfs,
                    "rounding": "symmetric half-up in integer fixed point",
                    "clipped_samples": clipped,
                },
                "features": {
                    "gain_q16_min": min(gains) if gains else None,
                    "gain_q16_max": max(gains) if gains else None,
                    "raw_rms_q15_min": min(raw_rms) if raw_rms else None,
                    "raw_rms_q15_max": max(raw_rms) if raw_rms else None,
                    "target_rms_q15_min": min(target_rms) if target_rms else None,
                    "target_rms_q15_max": max(target_rms) if target_rms else None,
                    "pcm_frame_sha256": sha256_json(
                        [row["energy"]["pcm_sha256"] for row in rows]
                    ),
                },
            },
        ]

    def _concat_utterance_task(
        self, spec: UtteranceSpec, ordered_results: list[dict[str, Any]]
    ) -> dict[str, Any]:
        config = self.inputs.config  # type: ignore[union-attr]
        ordered_results = sorted(ordered_results, key=lambda item: item["stable_key"])
        frame_entries = []
        for result in ordered_results:
            for row in result["frames"]:
                frame_entries.append(
                    {
                        "global_frame": row["global_frame"],
                        "feature_key": row["feature_key"],
                        "energy_key": row["energy_key"],
                        "pcm_sha256": row["energy"]["pcm_sha256"],
                    }
                )
        cache_key = concat_cache_key(
            utterance_id=spec.utterance_id,
            config=config,
            frame_entries=frame_entries,
        )
        loaded = next(
            utterance
            for utterance in self.inputs.utterances  # type: ignore[union-attr]
            if utterance.utterance_id == spec.utterance_id
        )
        utterance_edit_seqs: list[int] = []
        for phoneme in spec.phonemes:
            utterance_edit_seqs.extend(phoneme.f0_edit_seqs)
            utterance_edit_seqs.extend(phoneme.energy_edit_seqs)
            utterance_edit_seqs.extend(phoneme.voicing_edit_seqs)
        utterance_cache_key = sha256_json(
            {
                "acoustic_hash": config.acoustic_hash,
                "utterance_id": spec.utterance_id,
                "audio_hash": loaded.audio_hash,
                "label_hash": loaded.label_hash,
                "edit_history": edit_digest(utterance_edit_seqs),
                "frame_entries_sha256": sha256_json(frame_entries),
            }
        )
        total_frames = spec.total_frames
        output_length = (
            ((total_frames - 1) * config.frame_shift_samples + config.frame_length_samples)
            if total_frames > 0
            else 0
        )
        with self.cache_lock:
            cached = self.cache["concat_pcm"].get(cache_key)
        if cached is not None:
            pcm = np.asarray(cached["pcm"], dtype=np.int16)
            concat_hit = True
        else:
            accumulator = np.zeros(output_length, dtype=np.int64)
            counts = np.zeros(output_length, dtype=np.int64)
            for result in ordered_results:
                for row in result["frames"]:
                    start = int(row["global_frame"]) * config.frame_shift_samples
                    frame_pcm = np.asarray(row["pcm"], dtype=np.int64)
                    accumulator[start : start + config.frame_length_samples] += frame_pcm
                    counts[start : start + config.frame_length_samples] += 1
            pcm_int64 = _round_divide(accumulator, counts)
            pcm = np.clip(pcm_int64, -32768, 32767).astype(np.int16)
            cache_entry = {
                "pcm": [int(value) for value in pcm.tolist()],
                "sha256": pcm_sha256(pcm),
                "frame_count": total_frames,
            }
            with self.cache_lock:
                self.cache["concat_pcm"].setdefault(cache_key, cache_entry)
            concat_hit = False

        f0_hz: list[float] = []
        voiced: list[int] = []
        bands: list[list[float]] = []
        frame_keys: list[str] = []
        energy_keys: list[str] = []
        frame_energy_db: list[float] = []
        for result in ordered_results:
            for row in result["frames"]:
                f0_hz.append(float(row["feature"]["f0_hz"]))
                voiced.append(1 if row["feature"]["voiced"] else 0)
                bands.append([float(value) for value in row["feature"]["spectral_bands"]])
                frame_keys.append(row["feature_key"])
                energy_keys.append(row["energy_key"])
                start = int(row["global_frame"]) * config.frame_shift_samples
                segment = pcm[start : start + config.frame_length_samples]
                frame_energy_db.append(self._pcm_db(segment))

        return {
            "stable_key": (spec.utterance_id, 0, 0),
            "utterance_id": spec.utterance_id,
            "pcm": pcm,
            "pcm_sha256": pcm_sha256(pcm),
            "concat_key": cache_key,
            "utterance_cache_key": utterance_cache_key,
            "concat_hit": concat_hit,
            "frame_entries": frame_entries,
            "f0_hz": f0_hz,
            "voiced": voiced,
            "band_amplitudes": bands,
            "frame_keys": frame_keys,
            "energy_keys": energy_keys,
            "frame_energy_db": frame_energy_db,
            "phoneme_results": ordered_results,
            "total_frames": total_frames,
            "substate_position": "DONE",
        }

    def _write_utterance_output(
        self,
        *,
        config: AcousticConfig,
        concat_result: dict[str, Any],
        output_dir: Path,
    ) -> dict[str, Any]:
        utterance_id = concat_result["utterance_id"]
        wav_path = output_dir / f"{utterance_id}.wav"
        npz_path = output_dir / f"{utterance_id}.npz"
        params_path = output_dir / f"{utterance_id}.params.json"

        tmp_wav = wav_path.with_name(f".{wav_path.name}.tmp.{os.getpid()}")
        try:
            sf.write(
                str(tmp_wav),
                np.asarray(concat_result["pcm"], dtype=np.int16),
                config.sample_rate,
                subtype="PCM_16",
                format="WAV",
            )
            with tmp_wav.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(tmp_wav, wav_path)
        finally:
            if tmp_wav.exists():
                tmp_wav.unlink(missing_ok=True)

        tmp_npz = npz_path.with_name(f".{npz_path.name}.tmp.{os.getpid()}")
        try:
            with tmp_npz.open("wb") as handle:
                np.savez_compressed(
                    handle,
                    pcm=np.asarray(concat_result["pcm"], dtype=np.int16),
                    f0_hz=np.asarray(concat_result["f0_hz"], dtype=np.float64),
                    voiced=np.asarray(concat_result["voiced"], dtype=np.int8),
                    band_amplitudes=np.asarray(concat_result["band_amplitudes"], dtype=np.float64),
                    frame_energy_db=np.asarray(concat_result["frame_energy_db"], dtype=np.float64),
                    frame_keys=np.asarray(concat_result["frame_keys"], dtype=np.str_),
                    energy_keys=np.asarray(concat_result["energy_keys"], dtype=np.str_),
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_npz, npz_path)
        finally:
            if tmp_npz.exists():
                tmp_npz.unlink(missing_ok=True)

        params = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "utterance_id": utterance_id,
            "config_hash": config.config_hash,
            "sample_rate": config.sample_rate,
            "frame_length_samples": config.frame_length_samples,
            "frame_shift_samples": config.frame_shift_samples,
            "frame_count": concat_result["total_frames"],
            "pcm_samples": int(np.asarray(concat_result["pcm"]).size),
            "pcm_sha256": concat_result["pcm_sha256"],
            "concat_cache_key": concat_result["concat_key"],
            "utterance_cache_key": concat_result["utterance_cache_key"],
            "concat_cache_hit": concat_result["concat_hit"],
            "f0_hz_sha256": sha256_json(concat_result["f0_hz"]),
            "band_amplitudes_sha256": sha256_json(concat_result["band_amplitudes"]),
            "frame_energy_db_sha256": sha256_json(concat_result["frame_energy_db"]),
        }
        atomic_write_json(params_path, params)
        return {
            "utterance_id": utterance_id,
            "wav": str(wav_path),
            "params_npz": str(npz_path),
            "params_json": str(params_path),
            "pcm_sha256": concat_result["pcm_sha256"],
            "concat_cache_hit": concat_result["concat_hit"],
        }

    def _checkpoint(
        self,
        state: dict[str, Any],
        *,
        stage: str,
        completed: list[str],
        active: str | None,
        substate_positions: dict[str, str],
        partial_summary: dict[str, Any],
    ) -> None:
        config = self.inputs.config  # type: ignore[union-attr]
        state["stage"] = stage
        state["config_hash"] = config.config_hash
        state["label_hash"] = self.inputs.label_hash  # type: ignore[union-attr]
        state["completed_phonemes"] = sorted(set(completed))
        state["active_phoneme"] = active
        state["substate_positions"] = dict(sorted(substate_positions.items()))
        state["partial_feature_summary"] = partial_summary
        state["cache"] = self.cache
        state["failures"] = [failure.to_dict() for failure in self.failures]
        state["command_records"] = [record.to_dict() for record in self.command_records if record.status != "skipped_duplicate"]
        state["command_cursor"] = max(
            (record.seq for record in self.command_records if record.status != "skipped_duplicate"),
            default=-1,
        )
        next_by_stage = {
            "load_validate": "VOICING_DECISION",
            "phoneme_features": "VOICING_DECISION",
            "frame_concat": "FRAME_CONCAT",
            "writeback": "WRITE",
            "complete": "DONE",
        }
        state["recovery"] = {
            "next_stage": stage,
            "next_substate": next_by_stage.get(stage, "VOICING_DECISION"),
            "completed_phonemes": state["completed_phonemes"],
            "active_phoneme": active,
        }
        self.snapshot_store.save(state)
        self.checkpoint_count += 1
        if self.crash_after_checkpoints and self.checkpoint_count >= self.crash_after_checkpoints:
            os._exit(137)

    def _base_report(self, *, status: str, config: AcousticConfig) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "status": status,
            "config": config.report_dict(),
            "config_hash": config.config_hash,
            "label_hash": self.inputs.label_hash if self.inputs else None,
            "input_dir": str(self.input_dir),
            "work_dir": str(self.work_dir),
            "recovery_point": self.recovery_point,
            "snapshot_stage": self.resumed_snapshot_stage,
            "failures": [],
            "business_codes": [],
            "business_code_summary": [],
            "command_application_records": [record.to_dict() for record in self.command_records],
            "utterances": [],
            "invalidations": [],
            "reuse_ranges": [],
            "diagnostics": {},
        }

    def _build_complete_report(
        self,
        *,
        config: AcousticConfig,
        phoneme_results: dict[str, dict[str, Any]],
        concat_results: dict[str, dict[str, Any]],
        outputs: dict[str, dict[str, Any]],
        completed_phonemes: list[str],
        cache_stats: dict[str, int],
    ) -> dict[str, Any]:
        report = self._base_report(status="complete", config=config)
        report["failures"] = [failure.to_dict() for failure in self.failures]
        report["business_code_summary"] = self._business_code_summary(
            self.failures, self.command_records
        )
        report["business_codes"] = [item["code"] for item in report["business_code_summary"]]

        invalidation_items: list[dict[str, Any]] = []
        reuse_items: list[dict[str, Any]] = []
        voicing_counts = {"voiced": 0, "unvoiced": 0, "tie_voiced": 0}
        f0_distribution: dict[str, int] = {}
        utterances_report: list[dict[str, Any]] = []
        for utterance_id in sorted(concat_results):
            concat = concat_results[utterance_id]
            spec = self.specs[utterance_id]
            phoneme_reports = []
            for phoneme in spec.phonemes:
                result = phoneme_results[self._phoneme_identity_from_spec(spec, phoneme)]
                phoneme_reports.append(
                    {
                        "phoneme_index": result["phoneme_index"],
                        "phone": result["phone"],
                        "stable_sort_key": list(result["stable_key"]),
                        "phoneme_key": result["phoneme_key"],
                        "frame_start": result["frame_start"],
                        "frame_count": result["frame_count"],
                        "substate_position": result["substate_position"],
                        "states": result["state_trace"],
                        "intermediate_feature_summary": {
                            "f0_sha256": sha256_json(result["f0_hz"]),
                            "spectral_bands_sha256": sha256_json(result["spectral_bands"]),
                            "frame_energy_db_sha256": sha256_json(result["frame_energy_db"]),
                        },
                        "frame_intervals": result["intervals"],
                        "cache": result["cache"],
                        "reuse_ranges": result["reuse_ranges"],
                        "invalidation_ranges": result["invalidation_ranges"],
                        "invalidated_states": result["invalidated_states"],
                    }
                )
                if result["reuse_ranges"]:
                    reuse_items.append(
                        {
                            "utterance_id": utterance_id,
                            "phoneme_index": result["phoneme_index"],
                            "phone": result["phone"],
                            "ranges": result["reuse_ranges"],
                        }
                    )
                if result["invalidation_ranges"]:
                    invalidation_items.append(
                        {
                            "utterance_id": utterance_id,
                            "phoneme_index": result["phoneme_index"],
                            "phone": result["phone"],
                            "ranges": result["invalidation_ranges"],
                            "states": result["invalidated_states"],
                        }
                    )

                for local_index, voiced in enumerate(result["voiced"]):
                    voicing_counts["voiced" if voiced else "unvoiced"] += 1
                    decision = result["frames"][local_index]["feature"]["voicing_decision"]
                    if decision["score"] == decision["threshold"]:
                        voicing_counts["tie_voiced"] += 1
                for f0_hz in result["f0_hz"]:
                    if f0_hz > 0:
                        key = f"{f0_hz:.6f}"
                        f0_distribution[key] = f0_distribution.get(key, 0) + 1

            utterances_report.append(
                {
                    "utterance_id": utterance_id,
                    "frame_count": concat["total_frames"],
                    "phoneme_count": len(phoneme_reports),
                    "sorted_phoneme_order": [
                        item["stable_sort_key"] for item in phoneme_reports
                    ],
                    "phoneme_state_trajectories": phoneme_reports,
                    "voicing_counts": {
                        "voiced": sum(concat["voiced"]),
                        "unvoiced": len(concat["voiced"]) - sum(concat["voiced"]),
                    },
                    "f0_quantization_distribution": self._distribution_for_values(concat["f0_hz"]),
                    "concat_cache_key": concat["concat_key"],
                    "utterance_cache_key": concat["utterance_cache_key"],
                    "concat_cache_hit": concat["concat_hit"],
                    "pcm_sha256": concat["pcm_sha256"],
                    "outputs": outputs.get(utterance_id),
                }
            )

        report["utterances"] = utterances_report
        report["invalidations"] = invalidation_items
        report["reuse_ranges"] = reuse_items
        report["command_cursor"] = max(
            (record.seq for record in self.command_records if record.status != "skipped_duplicate"),
            default=-1,
        )
        report["command_application_records"] = [
            self._decorate_command_record(record, phoneme_results)
            for record in self.command_records
        ]
        report["diagnostics"] = {
            "voicing_counts": voicing_counts,
            "f0_quantization_distribution": dict(
                sorted(f0_distribution.items(), key=lambda item: float(item[0]))
            ),
            "cache": cache_stats,
            "phonemes_reused": sum(
                1 for result in phoneme_results.values() if result["cache"]["reused"]
            ),
            "phonemes_recomputed": sum(
                1 for result in phoneme_results.values() if not result["cache"]["reused"]
            ),
            "stable_task_order": [list(result["stable_key"]) for result in sorted(
                phoneme_results.values(), key=lambda item: item["stable_key"]
            )],
            "energy_accumulation_order": [list(result["stable_key"]) for result in sorted(
                phoneme_results.values(), key=lambda item: item["stable_key"]
            )],
            "failure_order": [
                {
                    "code": failure.code,
                    "scope": failure.scope,
                    "utterance_id": failure.utterance_id,
                    "path": failure.path,
                    "line": failure.line,
                }
                for failure in self.failures
            ],
            "concurrency": config.concurrency,
            "max_batch_frames": config.max_batch_frames,
            "snapshot_interval_frames": config.snapshot_interval_frames,
            "completed_phoneme_count": len(completed_phonemes),
        }
        report["files"] = {
            "state": str(self.snapshot_store.path),
            "output_dir": str(self.work_dir / "output"),
        }
        return report

    def _decorate_command_record(
        self,
        record: CommandRecord,
        phoneme_results: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        item = record.to_dict()
        if record.status != "applied":
            return item
        observations = []
        for target in record.affected_phonemes:
            utterance_id = target["utterance_id"]
            phoneme_index = target["phoneme_index"]
            for result in phoneme_results.values():
                if (
                    result["utterance_id"] == utterance_id
                    and result["phoneme_index"] == phoneme_index
                ):
                    observations.append(
                        {
                            "utterance_id": utterance_id,
                            "phoneme_index": phoneme_index,
                            "reuse_ranges": result["reuse_ranges"],
                            "invalidation_ranges": result["invalidation_ranges"],
                            "observed_states": result["invalidated_states"],
                        }
                    )
                    break
        item["observed_reuse_and_invalidation"] = observations
        return item

    def _business_code_summary(
        self,
        failures: list[Failure],
        command_records: list[CommandRecord],
    ) -> list[dict[str, Any]]:
        counts: dict[str, int] = {}
        scopes: dict[str, set[str]] = {}
        for failure in failures:
            counts[failure.code] = counts.get(failure.code, 0) + 1
            scopes.setdefault(failure.code, set()).add(failure.scope)
        failure_seq_codes = {
            (failure.code, failure.details.get("seq"))
            for failure in failures
            if "seq" in failure.details
        }
        for record in command_records:
            if record.code and (record.code, record.seq) not in failure_seq_codes:
                counts[record.code] = counts.get(record.code, 0) + 1
                scopes.setdefault(record.code, set()).add("command")
        return [
            {"code": code, "count": counts[code], "scopes": sorted(scopes[code])}
            for code in sorted(counts)
        ]

    def _distribution_for_values(self, values: list[float]) -> dict[str, int]:
        distribution: dict[str, int] = {}
        for value in values:
            if value > 0:
                key = f"{value:.6f}"
                distribution[key] = distribution.get(key, 0) + 1
        return dict(sorted(distribution.items(), key=lambda item: float(item[0])))

    def _partial_feature_summary(
        self, results: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            key: {
                "frame_count": result["frame_count"],
                "phoneme_key": result["phoneme_key"],
                "f0_sha256": sha256_json(result["f0_hz"]),
                "bands_sha256": sha256_json(result["spectral_bands"]),
                "substate_position": result["substate_position"],
            }
            for key, result in sorted(results.items())
        }

    def _partial_concat_summary(
        self, results: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            utterance_id: {
                "utterance_cache_key": result["utterance_cache_key"],
                "concat_cache_key": result["concat_key"],
                "pcm_sha256": result["pcm_sha256"],
                "frame_count": result["total_frames"],
                "substate_position": result["substate_position"],
            }
            for utterance_id, result in sorted(results.items())
        }

    def _interruption_recovery(self, stage: str, next_substate: str) -> dict[str, Any]:
        result = dict(self.recovery_point or {})
        result.update(
            {
                "interrupted_stage": stage,
                "next_substate": next_substate,
                "resume_source": str(self.snapshot_store.path),
                "checkpoint_count": self.checkpoint_count,
            }
        )
        return result

    def _finish_report(self, report: dict[str, Any], *, status: str, exit_code: int) -> RenderResult:
        report["status"] = status
        report["failures"] = [failure.to_dict() for failure in self.failures]
        report["business_code_summary"] = self._business_code_summary(
            self.failures, self.command_records
        )
        report["business_codes"] = [item["code"] for item in report["business_code_summary"]]
        report["command_application_records"] = [
            record.to_dict() for record in self.command_records
        ]
        path = self._write_report(report, name="report.json")
        return RenderResult(status, exit_code, report, path)

    def _write_report(self, report: dict[str, Any], *, name: str) -> Path:
        path = self.work_dir / name
        atomic_write_json(path, report)
        return path

    def _rejection_report(
        self,
        exc: SnapshotMismatch,
        *,
        snapshot: dict[str, Any] | None,
        config: AcousticConfig,
    ) -> RenderResult:
        report = self._base_report(status="rejected", config=config)
        report["failures"] = [
            {
                "code": exc.code,
                "scope": "snapshot",
                "message": exc.message,
                "details": exc.details,
            }
        ]
        report["business_codes"] = [exc.code]
        report["business_code_summary"] = [
            {"code": exc.code, "count": 1, "scopes": ["snapshot"]}
        ]
        report["recovery_point"] = {
            "source": "snapshot_rejected",
            "path": str(self.snapshot_store.path),
            "stage": snapshot.get("stage") if snapshot else None,
        }
        path = self._write_report(report, name="report.rejected.json")
        return RenderResult("rejected", 78, report, path)

    def _early_failure(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any],
        report_name: str,
        exit_code: int,
    ) -> RenderResult:
        report = {
            "schema_version": 1,
            "engine_version": ENGINE_VERSION,
            "status": "failed",
            "input_dir": str(self.input_dir),
            "work_dir": str(self.work_dir),
            "failures": [
                {
                    "code": code,
                    "scope": "config",
                    "message": message,
                    "details": details,
                }
            ],
            "business_codes": [code],
            "business_code_summary": [{"code": code, "count": 1, "scopes": ["config"]}],
            "utterances": [],
            "command_application_records": [],
            "invalidations": [],
            "reuse_ranges": [],
            "diagnostics": {},
        }
        path = self._write_report(report, name=report_name)
        return RenderResult("failed", exit_code, report, path)

    def _normalise_cache(self, cache: Any) -> dict[str, dict[str, Any]]:
        result = {
            "frame_features": {},
            "energy_frames": {},
            "concat_pcm": {},
        }
        if not isinstance(cache, dict):
            return result
        for key in result:
            value = cache.get(key)
            if isinstance(value, dict):
                result[key] = value
        return result

    def _record_from_dict(self, item: dict[str, Any]) -> CommandRecord:
        return CommandRecord(
            seq=int(item.get("seq", -1)),
            op=str(item.get("op", "")),
            utterance_id=item.get("utterance_id"),
            phoneme_index=item.get("phoneme_index"),
            status=str(item.get("status", "unknown")),
            code=item.get("code"),
            message=item.get("message"),
            affected_phonemes=list(item.get("affected_phonemes", [])),
            invalidated_states=list(item.get("invalidated_states", [])),
            reused_range=item.get("reused_range"),
            payload=dict(item.get("payload", {})),
            path=item.get("path"),
            line=item.get("line"),
        )

    def _phoneme_identity(self, result: dict[str, Any]) -> str:
        return f"{result['utterance_id']}:{result['phoneme_index']:04d}"

    def _phoneme_identity_from_spec(self, spec: UtteranceSpec, phoneme: PhonemeSpec) -> str:
        return f"{spec.utterance_id}:{phoneme.index:04d}"

    def _task_identity(
        self, task: tuple[tuple[str, int, int], LoadedUtterance, PhonemeSpec, int]
    ) -> str:
        key = task[0]
        return f"{key[0]}:{key[1]:04d}"

    @staticmethod
    def _pcm_db(pcm: np.ndarray) -> float:
        if pcm.size == 0:
            return -120.0
        values = pcm.astype(np.float64)
        rms = float(np.sqrt(np.mean(values * values)))
        if rms <= 0.0:
            return -120.0
        return 20.0 * math.log10(rms / 32768.0)
