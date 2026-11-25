"""Four-stage deterministic pipeline with atomic snapshots and cache reuse."""

from __future__ import annotations

import csv
import io
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import (
    GENESIS_HASH,
    ContentAddressedCache,
    block_source_hash,
    envelope_cache_key,
)
from .config import EngineConfig
from .drift import (
    compare_serial_whole,
    detect_drift_session,
    load_truth,
    replay_all,
)
from .envelope import (
    block_bounds,
    block_count,
    compute_envelope_block,
    compute_envelope_whole,
    estimate_envelope_memory,
    load_pcm,
)
from .errors import (
    CACHE_REBUILT_WHOLE,
    CACHE_REUSED,
    E_FORMAT_CORRUPT,
    E_RESOURCE_LIMIT,
    E_STATE_INCONSISTENT,
    M_V1_TO_V2_OK,
    M_V2_TO_V1_OK,
    OK,
    R_DEGRADED_SERIAL,
    S_STAGE_COMMITTED,
    S_STAGE_REUSED,
    EngineError,
    SimulatedCrash,
)
from .rules import (
    RuleSet,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
    read_rules,
)
from .state import CommitGate, EngineStore, STAGES
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    file_sha256,
    load_json_file,
    object_hash,
    require_field,
    require_list,
    require_mapping,
)


@dataclass
class StageOutcome:
    name: str
    record: dict[str, Any]
    payload: Any
    reused: bool
    business_code: str


@dataclass
class PipelineRun:
    stages: dict[str, StageOutcome]
    report: dict[str, Any]
    recovery: dict[str, Any] | None = None

    @property
    def business_codes(self) -> list[str]:
        return list(self.report.get("business_codes", []))

    def summary(self) -> dict[str, Any]:
        return {
            "business_code": self.report.get("business_code", OK),
            "business_codes": self.business_codes,
            "stage_versions": {
                name: outcome.record["version"] for name, outcome in self.stages.items()
            },
            "stage_reused": {
                name: outcome.reused for name, outcome in self.stages.items()
            },
            "sessions": len(self.report.get("sessions", [])),
            "blocks": int(self.report.get("totals", {}).get("blocks", 0)),
            "drift_events": int(self.report.get("totals", {}).get("drift_events", 0)),
            "verification": self.report.get("verification", {}),
            "recovery": self.recovery,
            "report_dir": self.report.get("report_dir"),
        }


class Pipeline:
    def __init__(
        self,
        *,
        data_dir: str | Path,
        state_dir: str | Path,
        report_dir: str | Path,
        config: EngineConfig | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.state_dir = Path(state_dir)
        self.report_dir = Path(report_dir)
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self.config = config or EngineConfig()
        self.config.validate()
        self.store = EngineStore(self.state_dir)
        self.gate = CommitGate(self.state_dir / "commit.lock")
        manifest = require_mapping(
            load_json_file(self.data_dir / "manifest.json", context="sample manifest"),
            "sample manifest",
        )
        if manifest.get("format") != "vad-sample-set/v1":
            raise EngineError(
                E_FORMAT_CORRUPT,
                "unsupported sample-set manifest",
                {"format": manifest.get("format")},
            )
        self.manifest = manifest
        sessions = require_list(require_field(manifest, "sessions", "sample manifest"), "sessions")
        if not sessions:
            raise EngineError(E_FORMAT_CORRUPT, "sample manifest contains no sessions")
        self.sessions: list[dict[str, Any]] = [
            require_mapping(item, f"sessions[{index}]") for index, item in enumerate(sessions)
        ]
        self.sessions.sort(key=lambda item: str(item["session_id"]))
        self.utterance_ids = sorted(
            utterance_id
            for session in self.sessions
            for utterance_id in require_list(
                require_field(session, "utterance_ids", "sample manifest session"),
                "utterance_ids",
            )
        )
        self._rules: dict[str, tuple[RuleSet, RuleSet]] = {}
        self._pcm_hashes: dict[str, str] = {}

    @property
    def expected_utterances(self) -> set[str]:
        return set(self.utterance_ids)

    def _session_paths(self, session_id: str) -> dict[str, str]:
        return {
            "pcm": f"pcm/{session_id}.pcm",
            "truth": f"truth/{session_id}.jsonl",
            "meta": f"meta/{session_id}.json",
            "baseline_rules": f"rules/v1/{session_id}.json",
            "current_rules": f"rules/v2/{session_id}",
        }

    def _rules_for(self, session_id: str) -> tuple[RuleSet, RuleSet]:
        if session_id not in self._rules:
            paths = self._session_paths(session_id)
            v1 = read_rules(self.data_dir / paths["baseline_rules"])
            v2 = read_rules(self.data_dir / paths["current_rules"])
            self._rules[session_id] = (v1, v2)
        return self._rules[session_id]

    def _pcm_hash_for(self, session_id: str) -> str:
        if session_id not in self._pcm_hashes:
            self._pcm_hashes[session_id] = file_sha256(
                self.data_dir / self._session_paths(session_id)["pcm"]
            )
        return self._pcm_hashes[session_id]

    def _token(self, stage: str, fingerprint: str) -> str:
        return object_hash(
            {
                "stage": stage,
                "fingerprint": fingerprint,
                "pid": os.getpid(),
                "nonce": uuid.uuid4().hex,
            }
        )

    def _find_stage(
        self, stage: str, fingerprint: str
    ) -> tuple[dict[str, Any], Any] | None:
        record = self.store.latest_snapshot(stage, fingerprint)
        if record is None:
            return None
        expected = self.expected_utterances
        completed = set(record["completed_utterances"])
        if completed != expected:
            raise EngineError(
                E_STATE_INCONSISTENT,
                "committed stage snapshot has an incomplete utterance set",
                {
                    "stage": stage,
                    "version": record["version"],
                    "missing": sorted(expected - completed),
                    "unexpected": sorted(completed - expected),
                },
            )
        payload = self.store.load_snapshot_payload(record)
        return record, payload

    def _parallel_policy(self) -> tuple[int, dict[str, Any] | None]:
        requested = self.config.parallel_chunks
        limit = self.config.max_parallel_chunks
        if requested <= limit:
            return max(1, requested), None
        if self.config.strict_resources:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "requested parallel chunk count exceeds the hard budget",
                {
                    "requested": requested,
                    "limit": limit,
                    "priority": "parallel_chunks: hard failure in strict mode",
                },
            )
        return 1, {
            "business_code": R_DEGRADED_SERIAL,
            "category": "PARALLEL_TO_SERIAL",
            "requested": requested,
            "limit": limit,
            "effective": 1,
        }

    def _compute_blocks(
        self,
        samples: Any,
        bounds: list[tuple[int, int, int]],
        missing_indices: list[int],
        *,
        pcm_sha256: str,
        workers: int,
    ) -> dict[int, dict[str, Any]]:
        if workers <= 1:
            return {
                index: compute_envelope_block(
                    samples,
                    self.config,
                    pcm_sha256=pcm_sha256,
                    block_index=index,
                )
                for index in missing_indices
            }

        def compute(index: int) -> tuple[int, dict[str, Any]]:
            return (
                index,
                compute_envelope_block(
                    samples,
                    self.config,
                    pcm_sha256=pcm_sha256,
                    block_index=index,
                ),
            )

        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vad-envelope") as pool:
            results = list(pool.map(compute, missing_indices))
        return dict(results)

    def _ensure_envelope(
        self, *, crash_after_blocks: int | None = None
    ) -> StageOutcome:
        rule_versions = {
            session["session_id"]: (
                f"{self._rules_for(session['session_id'])[0].version_id}|"
                f"{self._rules_for(session['session_id'])[1].version_id}"
            )
            for session in self.sessions
        }
        fingerprint = object_hash(
            {
                "stage": "envelope",
                "config": self.config.fingerprint(),
                "pcm_hashes": {
                    session["session_id"]: self._pcm_hash_for(session["session_id"])
                    for session in self.sessions
                },
                "rule_versions": rule_versions,
                "sample_seed": self.manifest.get("seed"),
            }
        )
        existing = self._find_stage("envelope", fingerprint)
        if existing is not None:
            record, payload = existing
            return StageOutcome("envelope", record, payload, True, S_STAGE_REUSED)

        total_blocks = sum(
            block_count(int(session["sample_count"]), self.config) for session in self.sessions
        )
        estimated_memory = estimate_envelope_memory(total_blocks)
        if estimated_memory > self.config.max_envelope_memory_bytes:
            raise EngineError(
                E_RESOURCE_LIMIT,
                "resident envelope statistics exceed the configured memory budget",
                {
                    "estimated_bytes": estimated_memory,
                    "limit_bytes": self.config.max_envelope_memory_bytes,
                    "blocks": total_blocks,
                    "priority": "envelope_memory: hard failure",
                },
            )
        workers, degradation = self._parallel_policy()
        token = self._token("envelope", fingerprint)
        cache = ContentAddressedCache(self.store, self.config, token)
        session_payloads: dict[str, Any] = {}
        processed_blocks = 0
        for session in self.sessions:
            reused_before = len(cache.reused)
            affected_before = len(cache.affected)
            session_id = str(session["session_id"])
            paths = self._session_paths(session_id)
            samples, pcm_hash, _ = load_pcm(self.data_dir / paths["pcm"])
            if pcm_hash != self._pcm_hash_for(session_id):
                raise EngineError(
                    E_STATE_INCONSISTENT,
                    "PCM changed while the envelope stage was running",
                    {"session_id": session_id},
                )
            rule_version = rule_versions[session_id]
            bounds = block_bounds(len(samples), self.config)
            upstream_hashes: list[str] = []
            keys: list[str] = []
            for offset, (_, start, end) in enumerate(bounds):
                upstream = (
                    GENESIS_HASH
                    if offset == 0
                    else block_source_hash(
                        samples, bounds[offset - 1][1], bounds[offset - 1][2]
                    )
                )
                upstream_hashes.append(upstream)
                keys.append(
                    envelope_cache_key(
                        pcm_sha256=pcm_hash,
                        rule_version=rule_version,
                        config=self.config,
                        upstream_envelope_hash=upstream,
                    )
                )
            blocks: list[dict[str, Any] | None] = [None] * len(bounds)
            missing: list[int] = []
            for index, key in enumerate(keys):
                block_id = f"{session_id}:block:{index:06d}"
                cached = cache.lookup(key, block_id)
                if cached is not None:
                    blocks[index] = cached
                    processed_blocks += 1
                    if (
                        crash_after_blocks is not None
                        and processed_blocks >= crash_after_blocks
                    ):
                        raise SimulatedCrash(
                            f"injected crash after {processed_blocks} envelope blocks"
                        )
                else:
                    missing.append(index)
            computed = self._compute_blocks(
                samples,
                bounds,
                missing,
                pcm_sha256=pcm_hash,
                workers=workers,
            )
            for index in missing:
                block = computed[index]
                block_id = f"{session_id}:block:{index:06d}"
                cache.put(
                    keys[index],
                    block,
                    block_id=block_id,
                    pcm_sha256=pcm_hash,
                    rule_version=rule_version,
                    upstream_hash=upstream_hashes[index],
                )
                blocks[index] = block
                processed_blocks += 1
                if crash_after_blocks is not None and processed_blocks >= crash_after_blocks:
                    raise SimulatedCrash(
                        f"injected crash after {processed_blocks} envelope blocks"
                    )
            if any(block is None for block in blocks):
                raise EngineError(
                    E_STATE_INCONSISTENT,
                    "envelope stage left unfilled blocks",
                    {"session_id": session_id},
                )
            session_cache = {
                "reused_block_keys": list(cache.reused[reused_before:]),
                "affected_block_keys": list(cache.affected[affected_before:]),
                "reused_count": len(cache.reused) - reused_before,
                "affected_count": len(cache.affected) - affected_before,
                "cache_entry_limit": self.config.max_cache_entries,
            }
            session_cache["mode"] = (
                "REUSE" if session_cache["affected_count"] == 0 else "WHOLE_REPLACE"
            )
            session_payloads[session_id] = {
                "session_id": session_id,
                "pcm_sha256": pcm_hash,
                "pcm_path": paths["pcm"],
                "rule_version": rule_version,
                "sample_count": int(len(samples)),
                "block_count": len(bounds),
                "envelope": blocks,
                "cache": session_cache,
            }

        previous = self.store.latest_snapshot("envelope")
        cache_result = cache.result()
        replacement_mode = "REUSE" if cache_result["affected_count"] == 0 else "WHOLE_REPLACE"
        payload = {
            "stage": "envelope",
            "config": self.config.public_dict(),
            "sessions": session_payloads,
            "degradation": degradation,
            "replacement": {
                "mode": replacement_mode,
                "snapshot_contains_all_blocks": True,
                "replaced_previous_checksum": previous["checksum"] if previous else None,
                "replaced_previous_version": previous["version"] if previous else None,
            },
            "cache": cache_result,
            "business_codes": sorted(
                {
                    CACHE_REUSED if replacement_mode == "REUSE" else CACHE_REBUILT_WHOLE,
                    *(
                        [R_DEGRADED_SERIAL]
                        if degradation is not None
                        else []
                    ),
                }
            ),
        }
        record = self.store.commit_snapshot(
            stage="envelope",
            input_fingerprint=fingerprint,
            completed_utterances=self.utterance_ids,
            payload=payload,
            stage_token=token,
            business_code=S_STAGE_COMMITTED,
        )
        return StageOutcome("envelope", record, payload, False, S_STAGE_COMMITTED)

    def _ensure_align(self, envelope: StageOutcome) -> StageOutcome:
        rule_revisions = {
            session_id: {
                "v1": self._rules_for(session_id)[0].revision_hash,
                "v2": self._rules_for(session_id)[1].revision_hash,
            }
            for session_id in sorted(item["session_id"] for item in self.sessions)
        }
        fingerprint = object_hash(
            {
                "stage": "align",
                "envelope_checksum": envelope.record["checksum"],
                "rule_revisions": rule_revisions,
                "config": self.config.fingerprint(),
            }
        )
        existing = self._find_stage("align", fingerprint)
        if existing is not None:
            record, payload = existing
            return StageOutcome("align", record, payload, True, S_STAGE_REUSED)

        token = self._token("align", fingerprint)
        session_payloads: dict[str, Any] = {}
        business_codes: set[str] = set()
        for session in self.sessions:
            session_id = str(session["session_id"])
            baseline, current = self._rules_for(session_id)
            migration_root = self.report_dir / "migrations" / session_id
            migration_root.mkdir(parents=True, exist_ok=True)
            up = migrate_v1_to_v2(baseline, migration_root / "v1_to_v2")
            down = migrate_v2_to_v1(current, migration_root / "v2_to_v1.json")
            up_dict = up.to_dict()
            down_dict = down.to_dict()
            up_dict["migrated"]["path"] = f"migrations/{session_id}/v1_to_v2"
            down_dict["migrated"]["path"] = f"migrations/{session_id}/v2_to_v1.json"
            migration_report = {
                "session_id": session_id,
                "v1_to_v2": up_dict,
                "v2_to_v1": down_dict,
                "business_codes": sorted(
                    {
                        M_V1_TO_V2_OK,
                        M_V2_TO_V1_OK,
                        *down.dropped_counts.keys(),
                    }
                ),
            }
            report_path = migration_root / "migration_report.json"
            atomic_write_json(report_path, migration_report)
            business_codes.update(migration_report["business_codes"])
            session_payloads[session_id] = {
                "session_id": session_id,
                "v1_revision": baseline.revision_hash,
                "v2_revision": current.revision_hash,
                "v1_rule_count": len(baseline.rules),
                "v2_rule_count": len(current.rules),
                "v1_to_v2": up_dict,
                "v2_to_v1": down_dict,
                "migration_report": f"migrations/{session_id}/migration_report.json",
            }
        payload = {
            "stage": "align",
            "sessions": session_payloads,
            "business_codes": sorted(business_codes),
        }
        with self.gate.held(stage="align", owner=token):
            record = self.store.commit_snapshot(
                stage="align",
                input_fingerprint=fingerprint,
                completed_utterances=self.utterance_ids,
                payload=payload,
                stage_token=token,
                business_code=S_STAGE_COMMITTED,
            )
        return StageOutcome("align", record, payload, False, S_STAGE_COMMITTED)

    def _ensure_drift(
        self, envelope: StageOutcome, align: StageOutcome
    ) -> StageOutcome:
        fingerprint = object_hash(
            {
                "stage": "drift",
                "envelope_checksum": envelope.record["checksum"],
                "align_checksum": align.record["checksum"],
                "tolerance": {
                    "absolute": self.config.drift_abs_tolerance,
                    "relative": self.config.drift_rel_tolerance,
                },
                "fixed_scale": self.config.fixed_scale,
                "config": self.config.fingerprint(),
            }
        )
        existing = self._find_stage("drift", fingerprint)
        if existing is not None:
            record, payload = existing
            return StageOutcome("drift", record, payload, True, S_STAGE_REUSED)

        envelope_payload = require_mapping(envelope.payload, "envelope payload")
        envelope_sessions = require_mapping(
            require_field(envelope_payload, "sessions", "envelope payload"),
            "envelope sessions",
        )
        token = self._token("drift", fingerprint)
        all_events: list[dict[str, Any]] = []
        session_payloads: dict[str, Any] = {}
        precision_checks = 0
        for session in self.sessions:
            session_id = str(session["session_id"])
            paths = self._session_paths(session_id)
            samples, pcm_hash, _ = load_pcm(self.data_dir / paths["pcm"])
            envelope_entry = require_mapping(
                envelope_sessions[session_id], f"envelope session {session_id}"
            )
            serial_envelope = require_list(
                require_field(envelope_entry, "envelope", "envelope session"),
                "envelope blocks",
            )
            baseline, current = self._rules_for(session_id)
            truth = load_truth(self.data_dir / paths["truth"])
            meta = require_mapping(
                load_json_file(self.data_dir / paths["meta"], context="session metadata"),
                "session metadata",
            )
            serial = detect_drift_session(
                session_id=session_id,
                envelope=serial_envelope,
                pcm_samples=samples,
                baseline=baseline,
                current=current,
                truth_rows=truth,
                meta=meta,
                config=self.config,
                paths=paths,
            )
            whole_envelope = compute_envelope_whole(
                samples, self.config, pcm_sha256=pcm_hash
            )
            whole = detect_drift_session(
                session_id=session_id,
                envelope=whole_envelope,
                pcm_samples=samples,
                baseline=baseline,
                current=current,
                truth_rows=truth,
                meta=meta,
                config=self.config,
                paths=paths,
            )
            serial_whole = compare_serial_whole(
                serial.events, whole.events, session_id=session_id
            )
            serial_whole["serial_envelope_sha256"] = object_hash(serial_envelope)
            serial_whole["whole_envelope_sha256"] = object_hash(whole_envelope)
            serial_whole["envelope_bytes_equal"] = canonical_json_bytes(
                serial_envelope
            ) == canonical_json_bytes(whole_envelope)
            all_events.extend(serial.events)
            precision_checks += serial.precision_checks
            session_payloads[session_id] = {
                "session_id": session_id,
                "blocks": serial.blocks,
                "events": serial.events,
                "event_count": len(serial.events),
                "precision_checks": serial.precision_checks,
                "serial_whole": serial_whole,
                "drift_codes": sorted({event["drift_category"] for event in serial.events}),
            }
        replay = replay_all(all_events, self.data_dir, self.config)
        payload = {
            "stage": "drift",
            "sessions": session_payloads,
            "all_events": all_events,
            "verification": {
                "precision_checks": precision_checks,
                "dual_precision_consistent": True,
                "replay": replay,
                "serial_whole_consistent": True,
                "event_count": len(all_events),
            },
            "business_codes": sorted(
                {event["business_code"] for event in all_events} or {OK}
            ),
        }
        with self.gate.held(stage="drift", owner=token):
            record = self.store.commit_snapshot(
                stage="drift",
                input_fingerprint=fingerprint,
                completed_utterances=self.utterance_ids,
                payload=payload,
                stage_token=token,
                business_code=S_STAGE_COMMITTED,
            )
        return StageOutcome("drift", record, payload, False, S_STAGE_COMMITTED)

    def _build_report(
        self,
        envelope: StageOutcome,
        align: StageOutcome,
        drift: StageOutcome,
        *,
        recovery: dict[str, Any] | None,
    ) -> dict[str, Any]:
        env_payload = envelope.payload
        align_payload = align.payload
        drift_payload = drift.payload
        env_sessions = env_payload["sessions"]
        align_sessions = align_payload["sessions"]
        drift_sessions = drift_payload["sessions"]
        session_rows: list[dict[str, Any]] = []
        business_codes: set[str] = {OK, S_STAGE_COMMITTED}
        total_blocks = 0
        for session_id in sorted(env_sessions):
            env_data = env_sessions[session_id]
            align_data = align_sessions[session_id]
            drift_data = drift_sessions[session_id]
            total_blocks += int(env_data["block_count"])
            cache = env_data["cache"]
            business_codes.add(
                CACHE_REUSED if cache["affected_count"] == 0 else CACHE_REBUILT_WHOLE
            )
            business_codes.add(align_data["v1_to_v2"]["business_code"])
            business_codes.add(align_data["v2_to_v1"]["business_code"])
            business_codes.update(align_data["v2_to_v1"]["dropped_counts"].keys())
            business_codes.update(drift_data["drift_codes"])
            session_rows.append(
                {
                    "session_id": session_id,
                    "pcm_sha256": env_data["pcm_sha256"],
                    "rule_version": env_data["rule_version"],
                    "sample_count": env_data["sample_count"],
                    "block_count": env_data["block_count"],
                    "cache": {
                        "mode": cache["mode"],
                        "affected_blocks": cache["affected_block_keys"],
                        "reused_blocks": cache["reused_block_keys"],
                        "affected_count": cache["affected_count"],
                        "reused_count": cache["reused_count"],
                    },
                    "migration": {
                        "v1_to_v2": align_data["v1_to_v2"],
                        "v2_to_v1": align_data["v2_to_v1"],
                    },
                    "drift": {
                        "event_count": drift_data["event_count"],
                        "codes": drift_data["drift_codes"],
                        "serial_whole": drift_data["serial_whole"],
                    },
                }
            )
        business_codes.update(drift_payload["business_codes"])
        business_codes.update(align_payload["business_codes"])
        if recovery is not None:
            business_codes.add(str(recovery.get("business_code", OK)))
        if envelope.payload.get("degradation"):
            business_codes.add(envelope.payload["degradation"]["business_code"])
        report = {
            "format": "vad-engine-run-report/v1",
            "business_code": OK,
            "business_codes": sorted(business_codes),
            "seed": self.manifest.get("seed"),
            "config": self.config.public_dict(),
            "report_dir": str(self.report_dir),
            "totals": {
                "sessions": len(session_rows),
                "blocks": total_blocks,
                "drift_events": len(drift_payload["all_events"]),
            },
            "sessions": session_rows,
            "drift_events": drift_payload["all_events"],
            "stage_checksums": {
                "envelope": envelope.record["checksum"],
                "align": align.record["checksum"],
                "drift": drift.record["checksum"],
            },
            "verification": {
                "dual_precision_consistent": True,
                "replay": drift_payload["verification"]["replay"],
                "serial_whole_consistent": True,
                "migration_semantic_checks": sum(
                    session["migration"]["v1_to_v2"]["semantic_checks"]
                    + session["migration"]["v2_to_v1"]["semantic_checks"]
                    for session in session_rows
                ),
                "recovery": recovery,
            },
        }
        return report

    def _write_reports(self, report: dict[str, Any]) -> None:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.report_dir / "run_report.json", report)
        atomic_write_json(
            self.report_dir / "drift_report.json",
            {
                "format": "vad-drift-report/v1",
                "business_code": report["business_code"],
                "events": report["drift_events"],
            },
        )
        atomic_write_json(
            self.report_dir / "verification.json",
            {
                "format": "vad-verification-report/v1",
                "business_code": report["business_code"],
                "verification": report["verification"],
                "stage_checksums": report["stage_checksums"],
            },
        )
        csv_buffer = io.StringIO()
        writer = csv.writer(csv_buffer, lineterminator="\n")
        writer.writerow(
            [
                "event_id",
                "session_id",
                "block_index",
                "sample_start",
                "sample_end",
                "drift_category",
                "business_code",
                "rule_version",
                "precomputed_hash",
                "speaker_id",
                "subband_index",
                "gain_jump_id",
            ]
        )
        for event in report["drift_events"]:
            speaker = event.get("speaker_activity") or {}
            subband = event.get("short_term_subband") or {}
            jump = event.get("gain_jump") or {}
            writer.writerow(
                [
                    event["event_id"],
                    event["session_id"],
                    event["envelope_block"]["block_index"],
                    event["sample_interval"]["start"],
                    event["sample_interval"]["end"],
                    event["drift_category"],
                    event["business_code"],
                    event["rule_version"],
                    event["precomputed_hash"],
                    speaker.get("speaker_id", ""),
                    subband.get("band_index", ""),
                    jump.get("jump_id", ""),
                ]
            )
        atomic_write_bytes(
            self.report_dir / "drift_report.csv",
            csv_buffer.getvalue().encode("utf-8"),
        )
        if report["verification"].get("recovery") is not None:
            atomic_write_json(
                self.report_dir / "recovery_report.json",
                report["verification"]["recovery"],
            )
        summary_lines = [
            "# VAD Rule Engine Run",
            "",
            f"- business_code: `{report['business_code']}`",
            f"- seed: `{report['seed']}`",
            f"- sessions: `{report['totals']['sessions']}`",
            f"- envelope blocks: `{report['totals']['blocks']}`",
            f"- drift events: `{report['totals']['drift_events']}`",
            f"- replay: `{report['verification']['replay']['consistent']}`",
            f"- dual precision: `{report['verification']['dual_precision_consistent']}`",
            f"- serial/whole: `{report['verification']['serial_whole_consistent']}`",
            f"- recovery: `{bool((report['verification'].get('recovery') or {}).get('passed'))}`",
            "",
            "## Business Codes",
            "",
            *[f"- `{code}`" for code in report["business_codes"]],
            "",
        ]
        atomic_write_bytes(
            self.report_dir / "summary.md",
            "\n".join(summary_lines).encode("utf-8"),
        )

    def _ensure_report(
        self,
        envelope: StageOutcome,
        align: StageOutcome,
        drift: StageOutcome,
        *,
        recovery: dict[str, Any] | None,
    ) -> StageOutcome:
        fingerprint = object_hash(
            {
                "stage": "report",
                "envelope_checksum": envelope.record["checksum"],
                "align_checksum": align.record["checksum"],
                "drift_checksum": drift.record["checksum"],
                "recovery": recovery,
                "report_format": "vad-engine-run-report/v1",
            }
        )
        existing = self._find_stage("report", fingerprint)
        if existing is not None:
            record, payload = existing
            self._write_reports(payload)
            return StageOutcome("report", record, payload, True, S_STAGE_REUSED)
        report = self._build_report(
            envelope, align, drift, recovery=recovery
        )
        self._write_reports(report)
        token = self._token("report", fingerprint)
        record = self.store.commit_snapshot(
            stage="report",
            input_fingerprint=fingerprint,
            completed_utterances=self.utterance_ids,
            payload=report,
            stage_token=token,
            business_code=S_STAGE_COMMITTED,
        )
        return StageOutcome("report", record, report, False, S_STAGE_COMMITTED)

    def run(
        self,
        *,
        stop_after: str | None = None,
        crash_after_blocks: int | None = None,
        recovery: dict[str, Any] | None = None,
    ) -> PipelineRun:
        if stop_after is not None and stop_after not in STAGES:
            raise EngineError(E_FORMAT_CORRUPT, "unknown stop-after stage", {"stage": stop_after})
        envelope = self._ensure_envelope(crash_after_blocks=crash_after_blocks)
        outcomes: dict[str, StageOutcome] = {"envelope": envelope}
        if stop_after == "envelope":
            report = {
                "business_code": OK,
                "business_codes": [OK],
                "sessions": [],
                "totals": {"blocks": 0, "drift_events": 0},
                "verification": {},
                "stage_checksums": {"envelope": envelope.record["checksum"]},
                "report_dir": str(self.report_dir),
                "seed": self.manifest.get("seed"),
            }
            return PipelineRun(outcomes, report)
        align = self._ensure_align(envelope)
        outcomes["align"] = align
        if stop_after == "align":
            report = {
                "business_code": OK,
                "business_codes": [OK],
                "sessions": [],
                "totals": {"blocks": 0, "drift_events": 0},
                "verification": {},
                "stage_checksums": {
                    "envelope": envelope.record["checksum"],
                    "align": align.record["checksum"],
                },
                "report_dir": str(self.report_dir),
                "seed": self.manifest.get("seed"),
            }
            return PipelineRun(outcomes, report)
        drift = self._ensure_drift(envelope, align)
        outcomes["drift"] = drift
        if stop_after == "drift":
            report = {
                "business_code": OK,
                "business_codes": [OK],
                "sessions": [],
                "totals": {
                    "blocks": sum(
                        int(item["block_count"])
                        for item in envelope.payload["sessions"].values()
                    ),
                    "drift_events": len(drift.payload["all_events"]),
                },
                "verification": drift.payload["verification"],
                "stage_checksums": {
                    "envelope": envelope.record["checksum"],
                    "align": align.record["checksum"],
                    "drift": drift.record["checksum"],
                },
                "report_dir": str(self.report_dir),
                "seed": self.manifest.get("seed"),
            }
            return PipelineRun(outcomes, report)
        report_outcome = self._ensure_report(
            envelope, align, drift, recovery=recovery
        )
        outcomes["report"] = report_outcome
        return PipelineRun(outcomes, report_outcome.payload, recovery=recovery)

