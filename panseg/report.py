"""Atomic JSON validation report assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import E_BOUNDARY_SENSITIVE
from .pipeline import PipelineRunResult
from .snapshot import atomic_write_json


def build_report(result: PipelineRunResult, config_hash: str, exit_code: int) -> dict[str, Any]:
    boundary_probes = []
    threshold_sensitive: list[str] = []
    policy_sensitive: list[str] = []
    for scene_id, fusion in sorted(result.fusion_results.items()):
        for probe in fusion.boundary_probes:
            boundary_probes.append(probe.to_dict())
            if probe.sensitive:
                if probe.kind == "threshold_iou":
                    threshold_sensitive.append(probe.sort_key)
                else:
                    policy_sensitive.append(probe.sort_key)

    fused_summary = []
    conflict_records = []
    for scene_id, fusion in sorted(result.fusion_results.items()):
        fused_summary.append(
            {
                "scene_id": scene_id,
                "width": fusion.width,
                "height": fusion.height,
                "fused_instance_count": len(fusion.fused_instances),
                "fused_instances": [item.to_dict() for item in fusion.fused_instances],
                "dropped_instances": list(fusion.dropped_instances),
            }
        )
        conflict_records.extend(item.to_dict() for item in fusion.conflicts)

    failures = [item.to_dict() for item in result.failures]
    report = {
        "report_version": 1,
        "snapshot_version": result.stats.snapshot_version,
        "config_hash": config_hash,
        "instance_count": result.stats.total_instances,
        "decoded_instances": result.stats.decoded_instances,
        "reused_instances": result.stats.reused_instances,
        "invalidated_instances": result.stats.invalidated_instances,
        "reused_fusions": result.stats.reused_fusions,
        "recomputed_fusions": result.stats.recomputed_fusions,
        "downgrade_losses": [item.to_dict() for item in result.conversion_losses],
        "failures": failures,
        "failure_count": len(failures),
        "fused_scenes": fused_summary,
        "conflicts": conflict_records,
        "boundary_diagnostics": {
            "probes": boundary_probes,
            "threshold_sensitive": threshold_sensitive,
            "policy_sensitive": policy_sensitive,
            "sensitive_codes": [E_BOUNDARY_SENSITIVE] if (threshold_sensitive or policy_sensitive) else [],
        },
        "signaled_shutdown": result.signaled,
        "exit_code": exit_code,
    }
    return report


def write_report(path: str | Path, report: dict[str, Any]) -> None:
    atomic_write_json(Path(path), report)
