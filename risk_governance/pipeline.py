"""End-to-end deterministic governance pipeline."""

from __future__ import annotations

import signal
import time
from pathlib import Path
from typing import Any

from . import ALGORITHM_VERSION, SNAPSHOT_CONTRACT_VERSION
from .boundary import diagnose_boundaries
from .cache import (
    ContentAddressedCache,
    CurrentRefs,
    ReferenceChain,
    build_cache_key,
)
from .calibration import calibrate_batch, segment_parameter_version
from .canonical import (
    atomic_write_json,
    read_json,
    read_jsonl,
    sha256_file,
    sha256_json,
    write_jsonl,
)
from .errors import E_COVERAGE_GAP, make_error
from .events import build_event_plan, coverage_check, load_events
from .recovery import (
    STAGE_REPORT,
    STAGE_WINDOW_AND_BOUNDARY,
    RecoveryManager,
)
from .snapshots import SnapshotManager
from .trace import build_decision_traces, traces_by_batch


class PipelineInterrupted(Exception):
    def __init__(self, message: str, signum: int | str = "injected", injected: bool = False):
        super().__init__(message)
        self.signum = signum
        self.injected = injected


class GovernancePipeline:
    def __init__(
        self,
        *,
        workdir: str | Path,
        samples_path: str | Path,
        config_path: str | Path,
        events_path: str | Path | None = None,
        now_ms: int | None = None,
        interrupt_after_batch: int | None = None,
        strict: bool = False,
        seed: int | None = None,
        batch_delay_ms: int = 0,
    ):
        self.workdir = Path(workdir)
        self.samples_path = Path(samples_path)
        self.config_path = Path(config_path)
        self.events_path = Path(events_path) if events_path else None
        self.now_is_fixed = now_ms is not None
        self.now_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)
        self.interrupt_after_batch = interrupt_after_batch
        self.strict = strict
        self.seed = seed
        self.batch_delay_ms = int(batch_delay_ms)
        self.recovery = RecoveryManager(self.workdir)
        self.cache = ContentAddressedCache(self.workdir / "cache")
        self.refs = CurrentRefs(self.workdir / "cache" / "refs.json")
        self.chain = ReferenceChain(self.workdir / "cache")
        self.snapshots = SnapshotManager(self.workdir)
        self.state: dict[str, Any] | None = None
        self.recovery_info: dict[str, Any] = {"recovered": False, "cleaned": []}
        self.input_hashes: dict[str, str | None] = {}

    def _now(self) -> int:
        return self.now_ms

    def install_signal_handlers(self) -> None:
        def handler(signum: int, frame: Any) -> None:
            raise PipelineInterrupted(f"received signal {signum}", signum=signum)

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):
                pass
        if hasattr(signal, "SIGHUP"):
            try:
                signal.signal(signal.SIGHUP, handler)
            except (ValueError, OSError):
                pass

    def _input_hash(self, path: Path | None) -> str | None:
        if path and path.is_file():
            return sha256_file(path)
        return None

    def _generation_metadata(self) -> dict[str, Any]:
        metadata_path = self.samples_path.parent / "generation_metadata.json"
        if metadata_path.is_file():
            return read_json(metadata_path)
        return {}

    def run(self) -> dict[str, Any]:
        self.workdir.mkdir(parents=True, exist_ok=True)
        samples_hash = self._input_hash(self.samples_path)
        config_hash = self._input_hash(self.config_path)
        events_hash = self._input_hash(self.events_path)
        self.input_hashes = {
            "samples_sha256": samples_hash,
            "config_sha256": config_hash,
            "events_sha256": events_hash,
        }
        config = read_json(self.config_path)
        events = load_events(str(self.events_path) if self.events_path else None)
        rows = read_jsonl(self.samples_path)

        from .stats import compute_batch_statistics, cross_batch_checks, group_batches

        datasets = group_batches(rows)
        batch_ids = [dataset["batch_id"] for dataset in datasets]
        batch_cycles = {dataset["batch_id"]: dataset["cycle_id"] for dataset in datasets}
        event_plan = build_event_plan(events, batch_ids, batch_cycles, config)

        plan_fingerprint = sha256_json(
            {
                "samples_sha256": samples_hash,
                "config_sha256": config_hash,
                "events_sha256": events_hash,
                "algorithm_version": ALGORITHM_VERSION,
                "snapshot_contract_version": SNAPSHOT_CONTRACT_VERSION,
                "strict": self.strict,
            }
        )
        self.state, self.recovery_info = self.recovery.prepare_for_plan(plan_fingerprint, self._now())
        self.recovery.mark_stage(self.state, 1, "sample_index", self._now())

        refs_document = self.refs.load()
        current_refs = dict(refs_document.get("refs", {}))
        committed_keys = self.state.get("batch_cache_keys", {})
        desired_keys: dict[str, tuple[str, dict[str, Any], int]] = {}

        for dataset in datasets:
            batch_id = dataset["batch_id"]
            event_seq = int(event_plan["event_cursor_by_batch"].get(batch_id, 0))
            segment_version = segment_parameter_version(config, batch_id)
            key, key_inputs = build_cache_key(
                sample_hash=dataset["content_hash"],
                algorithm_version=ALGORITHM_VERSION,
                segment_parameter_version=segment_version,
                event_seq=event_seq,
                snapshot_version=SNAPSHOT_CONTRACT_VERSION,
                extra={"batch_id": batch_id, "cycle_id": dataset["cycle_id"]},
            )
            desired_keys[batch_id] = (key, key_inputs, event_seq)

        recompute_set: list[str] = []
        reused_set: list[str] = []
        for batch_id in batch_ids:
            key, _, _ = desired_keys[batch_id]
            existing_key = current_refs.get(f"batch:{batch_id}", {}).get("cache_key")
            existing_key = existing_key or committed_keys.get(batch_id)
            if existing_key == key and self.cache.has(key):
                reused_set.append(batch_id)
            else:
                recompute_set.append(batch_id)

        coverage = coverage_check(batch_ids, recompute_set, reused_set)

        artifacts: dict[str, dict[str, Any]] = {}
        computed_batch_ids: list[str] = []
        cache_reused_batch_ids: list[str] = []
        recovered_skipped_batch_ids: list[str] = []

        for dataset in datasets:
            batch_id = dataset["batch_id"]
            key, key_inputs, event_seq = desired_keys[batch_id]
            ref_name = f"batch:{batch_id}"
            existing_key = current_refs.get(ref_name, {}).get("cache_key") or committed_keys.get(batch_id)
            should_recompute = batch_id in recompute_set

            if not should_recompute:
                envelope = self.cache.get(key)
                if envelope is None:
                    should_recompute = True
                else:
                    artifacts[batch_id] = envelope["artifact"]
                    cache_reused_batch_ids.append(batch_id)
                    if batch_id in self.state.get("committed_batches", []):
                        recovered_skipped_batch_ids.append(batch_id)
                    continue

            if self.interrupt_after_batch is not None and not self.recovery_info.get("recovered"):
                committed_count = len(self.state.get("committed_batches", []))
                if (
                    committed_count >= int(self.interrupt_after_batch)
                    and batch_id not in self.state.get("committed_batches", [])
                ):
                    self.recovery.begin_batch(
                        batch_id,
                        key,
                        {"reason": "deterministic_interrupt_injection"},
                    )
                    self.recovery.update_intermediate(
                        batch_id,
                        {"status": "partial", "computed_fields": ["inflight_marker_only"]},
                    )
                    raise PipelineInterrupted(
                        f"injected after {self.interrupt_after_batch} committed batches",
                        signum=signal.SIGTERM,
                        injected=True,
                    )

            self.recovery.begin_batch(batch_id, key)
            if self.batch_delay_ms:
                time.sleep(self.batch_delay_ms / 1000.0)
            stats = compute_batch_statistics(dataset, config)
            self.recovery.update_intermediate(
                batch_id,
                {"status": "statistics_ready", "fingerprint": stats["fingerprint"]},
            )
            calibration, calibration_errors = calibrate_batch(stats, config)
            artifact = {
                "artifact_type": "batch_governance_v1",
                "batch_id": batch_id,
                "cycle_id": dataset["cycle_id"],
                "statistics": stats,
                "calibration": calibration,
                "batch_errors": calibration_errors,
            }
            self.cache.put(key, key_inputs, artifact)
            if existing_key != key:
                self.chain.append_change(
                    ref_name=ref_name,
                    old_key=existing_key,
                    new_key=key,
                    event_seq=event_seq,
                    reason="batch_key_changed",
                    snapshot_version=SNAPSHOT_CONTRACT_VERSION,
                    details={
                        "sample_content_hash": dataset["content_hash"],
                        "segment_parameter_version": segment_parameter_version(config, batch_id),
                    },
                )
            ref_value = {
                "cache_key": key,
                "event_seq": event_seq,
                "snapshot_version": SNAPSHOT_CONTRACT_VERSION,
            }
            self.refs.set_many({ref_name: ref_value})
            current_refs[ref_name] = ref_value
            safe_batch = batch_id.replace("/", "_")
            atomic_write_json(self.workdir / "ledger" / "batches" / f"{safe_batch}.json", artifact)
            self.recovery.commit_batch(self.state, batch_id, key, self._now())
            artifacts[batch_id] = artifact
            computed_batch_ids.append(batch_id)

        ordered_artifacts = [artifacts[batch_id] for batch_id in batch_ids]
        batch_stats = [artifact["statistics"] for artifact in ordered_artifacts]
        calibration_results = [artifact["calibration"] for artifact in ordered_artifacts]
        calibration_by_id = {artifact["batch_id"]: artifact["calibration"] for artifact in ordered_artifacts}
        calibration_errors = [error for artifact in ordered_artifacts for error in artifact["batch_errors"]]

        atomic_write_json(self.workdir / "ledger" / "batch_statistics_ledger.json", batch_stats)
        atomic_write_json(self.workdir / "ledger" / "calibration_ledger.json", calibration_results)

        self.recovery.mark_stage(
            self.state, STAGE_WINDOW_AND_BOUNDARY, "window_statistics_and_boundary", self._now()
        )
        reference_cycles = config.get("reference_cycle_ids", [])
        reference_stats = _compute_window(
            datasets,
            config,
            window_name="reference_window",
            cycle_ids=reference_cycles,
        )
        full_stats = _compute_window(datasets, config, window_name="full_window")
        checks = cross_batch_checks(batch_stats, config)

        boundary_diagnostics, boundary_errors = diagnose_boundaries(datasets, config)
        traces = build_decision_traces(datasets, calibration_by_id, config)
        trace_hash = sha256_json(traces)
        write_jsonl(self.workdir / "decision_traces.jsonl", traces)
        atomic_write_json(self.workdir / "boundary_diagnostics.json", boundary_diagnostics)
        atomic_write_json(
            self.workdir / "statistics_windows.json",
            {"reference_window": reference_stats, "full_window": full_stats, "cross_batch_checks": checks},
        )

        business_errors = list(calibration_errors) + list(boundary_errors)
        if not coverage["full_coverage"]:
            business_errors.append(
                make_error(
                    E_COVERAGE_GAP,
                    "recompute and reused sets do not cover all batches",
                    coverage=coverage,
                )
            )

        batch_entries = []
        batch_summaries = []
        for batch_id in batch_ids:
            artifact = artifacts[batch_id]
            stats = artifact["statistics"]
            key, _, event_seq = desired_keys[batch_id]
            entry = {
                "batch_id": batch_id,
                "cycle_id": stats["cycle_id"],
                "record_count": stats["record_count"],
                "sample_count": stats["sample_count"],
                "anomaly_count": stats["anomaly_count"],
                "anomaly_counts": stats["anomaly_counts"],
                "fingerprint": stats["fingerprint"],
                "content_hash": stats["content_hash"],
                "cache_key": key,
                "event_seq": event_seq,
                "calibration_status": artifact["calibration"]["status"],
                "error_codes": sorted({error["code"] for error in artifact["batch_errors"]}),
                "statistics": stats,
            }
            batch_entries.append(entry)
            batch_summaries.append({key: value for key, value in entry.items() if key != "statistics"})

        chain_verification = self.chain.verify()
        chain_entries = self.chain.entries()
        cache_ref_rows = [
            {"ref_name": ref_name, **value}
            for ref_name, value in sorted(current_refs.items())
            if isinstance(value, dict)
        ]

        snapshot_payload = {
            "schema": "risk-governance-snapshot-payload-v1",
            "config": config,
            "batch_summaries": batch_entries,
            "reference_statistics": reference_stats,
            "full_statistics": full_stats,
            "cross_batch_checks": checks,
            "calibration_results": calibration_results,
            "boundary_diagnostics": boundary_diagnostics,
            "errors": business_errors,
            "trace_count": len(traces),
            "trace_checksum": trace_hash,
        }
        v1_snapshot = self.snapshots.publish_v1(snapshot_payload, self._now())
        v2_snapshot = self.snapshots.publish_v2(
            snapshot_payload,
            self._now(),
            cache_refs=cache_ref_rows,
            traces_by_batch=traces_by_batch(traces),
            trace_rows=traces,
        )
        v2_verification = self.snapshots.verify_current_v2()

        self.recovery.mark_stage(self.state, STAGE_REPORT, "execution_report", self._now())
        metadata = self._generation_metadata()
        seed = self.seed if self.seed is not None else metadata.get("seed")
        report_body = {
            "report_version": "1.0",
            "checksum_algorithm": "sha256-canonical-json",
            "status": "completed_with_business_errors" if business_errors else "completed",
            "generated_at_ms": self._now(),
            "clock_mode": "fixed_replayable" if self.now_is_fixed else "wall_clock_record_now_ms_for_replay",
            "seed": seed,
            "replay_parameters": {
                "samples": str(self.samples_path),
                "config": str(self.config_path),
                "events": str(self.events_path) if self.events_path else None,
                "workdir": str(self.workdir),
                "now_ms": self._now(),
                "seed": seed,
            },
            "input_checksums": self.input_hashes,
            "counts": {
                "batch_count": len(batch_ids),
                "record_count": len(rows),
                "trace_count": len(traces),
                "anomaly_count": sum(batch["anomaly_count"] for batch in batch_stats),
                "business_error_count": len(business_errors),
            },
            "batch_summaries": batch_summaries,
            "window_statistics": {
                "reference_window": reference_stats,
                "full_window": full_stats,
            },
            "cross_batch_checks": checks,
            "event_plan": event_plan,
            "planned_recompute_set": recompute_set,
            "reused_set": reused_set,
            "computed_batch_ids": computed_batch_ids,
            "cache_reused_batch_ids": cache_reused_batch_ids,
            "recovered_skipped_batch_ids": recovered_skipped_batch_ids,
            "coverage_check": coverage,
            "event_closure_check": {
                "event_affected_closure": event_plan["affected_closure"],
                "event_closure_without_recompute": sorted(
                    set(event_plan["affected_closure"]) - set(recompute_set)
                ),
                "recompute_without_event_closure": sorted(
                    set(recompute_set) - set(event_plan["affected_closure"])
                ),
                "cold_start_or_parameter_change": not any(
                    ref.get("cache_key") for ref in current_refs.values()
                ),
            },
            "calibration_change_ledger": chain_entries,
            "cache_reference_chain": chain_verification,
            "boundary_summary": [
                {
                    "target": item["target"],
                    "flip_count": item["flip_count"],
                    "max_flips": item["max_flips"],
                    "trajectory_count": item["trajectory_count"],
                    "repair": item["repair"],
                    "fingerprint": item["fingerprint"],
                }
                for item in boundary_diagnostics
            ],
            "errors": business_errors,
            "error_codes": sorted({error["code"] for error in business_errors}),
            "snapshots": {"v1": v1_snapshot, "v2": v2_snapshot, "v2_verification": v2_verification},
            "recovery": {
                **self.recovery_info,
                "committed_batches": self.state.get("committed_batches", []),
                "committed_stage_index": self.state.get("committed_stage_index"),
                "stage_name": self.state.get("stage_name"),
            },
            "checksums": {
                **self.input_hashes,
                "traces_sha256": trace_hash,
                "v1_content_checksum": v1_snapshot["content_checksum"],
                "v2_content_checksum": v2_snapshot["content_checksum"],
            },
        }
        report_checksum = sha256_json(report_body)
        report = {
            **report_body,
            "checksum": report_checksum,
        }
        atomic_write_json(self.workdir / "reports" / "execution_report.json", report)
        self.recovery.complete(self.state, self._now())
        return report

    def persist_interruption(self, exc: PipelineInterrupted) -> dict[str, Any]:
        if self.state is None:
            self.state = self.recovery.load() or self.recovery.reset("unstarted", self._now())
        signum = exc.signum if isinstance(exc.signum, int) else str(exc.signum)
        self.recovery.persist_interrupt(self.state, signum, self._now())
        report = {
            "status": "interrupted",
            "interrupted": True,
            "injected": exc.injected,
            "signal": signum,
            "message": str(exc),
            "recovery": self.state,
            "checksum_algorithm": "sha256-canonical-json",
        }
        report["checksum"] = sha256_json({key: value for key, value in report.items() if key != "checksum"})
        atomic_write_json(self.workdir / "reports" / "interrupted_report.json", report)
        return report

    def execute(self) -> tuple[int, dict[str, Any]]:
        self.install_signal_handlers()
        try:
            report = self.run()
            if self.strict and report.get("error_codes"):
                return 2, report
            return 0, report
        except PipelineInterrupted as exc:
            report = self.persist_interruption(exc)
            signum = exc.signum if isinstance(exc.signum, int) else signal.SIGTERM
            return 128 + int(signum), report


def _compute_window(datasets: list[dict[str, Any]], config: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    from .stats import compute_window_statistics

    return compute_window_statistics(datasets, config, **kwargs)
