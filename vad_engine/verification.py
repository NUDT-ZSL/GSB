"""Recovery drill and cache whole-replacement verification."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from .config import EngineConfig
from .errors import SimulatedCrash
from .pipeline import Pipeline
from .util import atomic_write_json, load_json_file


def _envelope_versions(run: Any) -> list[int]:
    return [stage.record["version"] for stage in run.stages.values()]


def run_recovery_drill(
    *,
    data_dir: str | Path,
    config: EngineConfig,
) -> dict[str, Any]:
    data_root = Path(data_dir)
    with tempfile.TemporaryDirectory(prefix="vad-recovery-") as temp_name:
        temp = Path(temp_name)

        crash_state = temp / "crash_state"
        crash_reports = temp / "crash_reports"
        crashed = Pipeline(
            data_dir=data_root,
            state_dir=crash_state,
            report_dir=crash_reports,
            config=config,
        )
        crash_injected = False
        try:
            crashed.run(crash_after_blocks=17)
        except SimulatedCrash:
            crash_injected = True
        uncommitted_before_restart = crashed.store.uncommitted_cache_count()
        snapshots_after_crash = len(crashed.store.snapshot_history())
        restarted = Pipeline(
            data_dir=data_root,
            state_dir=crash_state,
            report_dir=crash_reports,
            config=config,
        )
        uncommitted_after_restart = restarted.store.uncommitted_cache_count()
        snapshots_after_restart = len(restarted.store.snapshot_history())
        crash_resume_run = restarted.run()
        crash_resume_versions = _envelope_versions(crash_resume_run)
        crash_pass = (
            crash_injected
            and uncommitted_before_restart > 0
            and uncommitted_after_restart == 0
            and snapshots_after_crash == 0
            and snapshots_after_restart == 0
            and crash_resume_versions == [1, 2, 3, 4]
        )

        resume_state = temp / "resume_state"
        resume_reports = temp / "resume_reports"
        first = Pipeline(
            data_dir=data_root,
            state_dir=resume_state,
            report_dir=resume_reports,
            config=config,
        )
        partial = first.run(stop_after="align")
        partial_versions = _envelope_versions(partial)
        envelope_checksum = partial.stages["envelope"].record["checksum"]
        align_checksum = partial.stages["align"].record["checksum"]
        resumed = Pipeline(
            data_dir=data_root,
            state_dir=resume_state,
            report_dir=resume_reports,
            config=config,
        )
        final = resumed.run()
        final_versions = _envelope_versions(final)
        committed_resume_pass = (
            partial_versions == [1, 2]
            and final_versions == [1, 2, 3, 4]
            and final.stages["envelope"].reused
            and final.stages["align"].reused
            and final.stages["envelope"].record["checksum"] == envelope_checksum
            and final.stages["align"].record["checksum"] == align_checksum
        )

        cache_state = temp / "cache_state"
        cache_reports = temp / "cache_reports"
        base_config = EngineConfig(
            **{
                **config.__dict__,
                "parallel_chunks": 1,
            }
        )
        changed_config = EngineConfig(
            **{
                **config.__dict__,
                "parallel_chunks": min(2, config.max_parallel_chunks),
                "max_cache_entries": config.max_cache_entries + 1,
            }
        )
        cache_pipeline = Pipeline(
            data_dir=data_root,
            state_dir=cache_state,
            report_dir=cache_reports,
            config=base_config,
        )
        first_cache_run = cache_pipeline.run(stop_after="envelope")
        first_cache = first_cache_run.stages["envelope"].payload["cache"]
        second_pipeline = Pipeline(
            data_dir=data_root,
            state_dir=cache_state,
            report_dir=cache_reports,
            config=changed_config,
        )
        second_cache_run = second_pipeline.run(stop_after="envelope")
        second_envelope = second_cache_run.stages["envelope"]
        second_cache = second_envelope.payload["cache"]
        reuse_pass = (
            second_cache["affected_count"] == 0
            and second_cache["reused_count"] == first_cache["reused_count"] + first_cache["affected_count"]
            and second_envelope.payload["replacement"]["mode"] == "REUSE"
        )

        revised_data = temp / "data_revised"
        shutil.copytree(data_root, revised_data)
        v1_path = revised_data / "rules" / "v1" / "session_001.json"
        v1_payload = load_json_file(v1_path, context="v1 rules")
        v1_payload["rules"][0]["energy_threshold"] = round(
            float(v1_payload["rules"][0]["energy_threshold"]) + 0.001, 12
        )
        atomic_write_json(v1_path, v1_payload)
        revised_pipeline = Pipeline(
            data_dir=revised_data,
            state_dir=cache_state,
            report_dir=cache_reports,
            config=changed_config,
        )
        revised_run = revised_pipeline.run(stop_after="envelope")
        revised_envelope = revised_run.stages["envelope"]
        revised_cache = revised_envelope.payload["cache"]
        replacement_pass = (
            revised_cache["affected_count"] > 0
            and revised_cache["reused_count"] > 0
            and revised_cache["affected_count"] + revised_cache["reused_count"]
            == second_cache["reused_count"]
            and revised_envelope.payload["replacement"]["mode"] == "WHOLE_REPLACE"
            and revised_envelope.payload["replacement"]["replaced_previous_version"]
            == second_envelope.record["version"]
        )

        passed = crash_pass and committed_resume_pass and reuse_pass and replacement_pass
        return {
            "format": "vad-recovery-drill/v1",
            "business_code": "OK" if passed else "E_STATE_INCONSISTENT",
            "passed": passed,
            "uncommitted_crash": {
                "passed": crash_pass,
                "crash_injected": crash_injected,
                "uncommitted_cache_before_restart": uncommitted_before_restart,
                "uncommitted_cache_after_restart": uncommitted_after_restart,
                "snapshots_after_crash": snapshots_after_crash,
                "snapshots_after_restart": snapshots_after_restart,
                "resumed_versions": crash_resume_versions,
            },
            "committed_resume": {
                "passed": committed_resume_pass,
                "partial_versions": partial_versions,
                "final_versions": final_versions,
                "envelope_reused": final.stages["envelope"].reused,
                "align_reused": final.stages["align"].reused,
                "envelope_checksum": envelope_checksum,
                "align_checksum": align_checksum,
            },
            "cache_reuse": {
                "passed": reuse_pass,
                "first_affected": first_cache["affected_count"],
                "second_affected": second_cache["affected_count"],
                "second_reused": second_cache["reused_count"],
                "mode": second_envelope.payload["replacement"]["mode"],
            },
            "cache_whole_replacement": {
                "passed": replacement_pass,
                "affected": revised_cache["affected_count"],
                "reused": revised_cache["reused_count"],
                "mode": revised_envelope.payload["replacement"]["mode"],
                "replaced_previous_version": revised_envelope.payload["replacement"][
                    "replaced_previous_version"
                ],
                "new_version": revised_envelope.record["version"],
            },
        }

