"""End-to-end deterministic demonstration and acceptance report."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .config import PROJECT_ROOT, default_data_dir, load_default_workspace
from .errors import BusinessError
from .formats import parse_description_file
from .generator import write_samples
from .graph import BuildResult, FusionGraph
from .injection import InjectionPlan
from .migration import (
    downgrade_v3_to_v2,
    downgrade_v2_to_v1,
    upgrade_v1_to_v2,
    upgrade_v2_to_v3,
    verify_behavior_preserved,
)
from .models import FlightState
from .serialization import content_hash
from .snapshot import SnapshotStore


def _with_snapshot(
    state: FlightState,
    version: int,
    checksum: str,
) -> FlightState:
    return replace(
        state,
        snapshot_version=version,
        snapshot_checksum=checksum,
    )


def _load_format_descriptions(data_dir: Path) -> dict[int, dict[str, Any]]:
    paths = {
        1: data_dir / "formats" / "sensors.v1.txt",
        2: data_dir / "formats" / "sensors.v2.csv",
        3: data_dir / "formats" / "sensors.v3.json",
    }
    result = {}
    for version, path in paths.items():
        parsed_version, descriptions = parse_description_file(path)
        if parsed_version != version:
            raise RuntimeError(f"expected v{version}, parsed v{parsed_version}")
        result[version] = descriptions
    return result


def _migration_report(data_dir: Path) -> dict[str, Any]:
    descriptions = _load_format_descriptions(data_dir)
    v1_to_v2 = upgrade_v1_to_v2(descriptions[1])
    v2_to_v3 = upgrade_v2_to_v3(descriptions[2])
    v3_to_v2 = downgrade_v3_to_v2(descriptions[3])
    v2_to_v1 = downgrade_v2_to_v1(v3_to_v2.descriptions)
    return {
        "v1_to_v2": {
            **v1_to_v2.as_dict(),
            "behavior_preserved": verify_behavior_preserved(
                descriptions[1], v1_to_v2.descriptions
            ),
        },
        "v2_to_v3": {
            **v2_to_v3.as_dict(),
            "behavior_preserved": verify_behavior_preserved(
                descriptions[2], v2_to_v3.descriptions
            ),
        },
        "v3_to_v2": {
            **v3_to_v2.as_dict(),
            "behavior_preserved": verify_behavior_preserved(
                descriptions[3], v3_to_v2.descriptions
            ),
        },
        "v2_to_v1": {
            **v2_to_v1.as_dict(),
            "behavior_preserved": verify_behavior_preserved(
                v3_to_v2.descriptions, v2_to_v1.descriptions
            ),
        },
    }


def _scenario_summary(
    name: str,
    description: str,
    incremental: BuildResult,
    full: BuildResult,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "snapshot_version": incremental.state.snapshot_version,
        "snapshot_checksum": incremental.state.snapshot_checksum,
        "recomputed_nodes": incremental.recomputed_nodes,
        "reused_nodes": incremental.reused_nodes,
        "affected_sensors": incremental.affected_sensors,
        "full_recompute_equal": incremental.output_bytes() == full.output_bytes(),
        "stats": incremental.output["stats"],
        "business_codes": incremental.business_codes,
    }


def _catch_snapshot_event(store: SnapshotStore, callback) -> dict[str, Any]:
    try:
        result = callback()
        return {
            "status": result.status,
            "version": result.snapshot.version,
            "checksum": result.snapshot.checksum,
            "code": None,
        }
    except BusinessError as exc:
        store.last_error_code = exc.code
        return {
            "status": "rejected",
            "version": None,
            "checksum": None,
            "code": exc.code,
            "message": exc.message,
        }


def run_demo(
    *,
    data_dir: str | Path | None = None,
    cache_dir: str | Path | None = None,
    artifact_path: str | Path | None = None,
    extra_injections: Iterable[str | dict[str, Any]] | None = None,
    fresh_cache: bool = True,
) -> dict[str, Any]:
    base_data = Path(data_dir) if data_dir is not None else default_data_dir()
    if not (base_data / "flight_imu.json").exists():
        write_samples(base_data.parent, force=False)
    cache_path = (
        Path(cache_dir)
        if cache_dir is not None
        else PROJECT_ROOT / ".cache" / "flightfusion-demo"
    )
    if fresh_cache and cache_path.exists():
        shutil.rmtree(cache_path)
    graph = FusionGraph(cache_path)

    base_state = load_default_workspace(base_data)
    store = SnapshotStore()
    snapshot_events: list[dict[str, Any]] = []
    created = store.commit(
        base_state.snapshot_payload(),
        version=1,
        provided_checksum=base_state.snapshot_checksum,
    )
    snapshot_events.append(
        {
            "status": created.status,
            "version": created.snapshot.version,
            "checksum": created.snapshot.checksum,
            "code": None,
        }
    )
    base_state = _with_snapshot(
        base_state, created.snapshot.version, created.snapshot.checksum
    )
    initial = graph.build(base_state, force_recompute=True)
    initial_full = graph.build(base_state, force_recompute=True)
    repeat = graph.build(base_state)
    cache_repeat_equal = initial.output_bytes() == repeat.output_bytes()

    snapshot_events.append(
        _catch_snapshot_event(
            store,
            lambda: store.commit(
                base_state.snapshot_payload(),
                version=1,
                provided_checksum=base_state.snapshot_checksum,
            ),
        )
    )
    snapshot_events.append(
        _catch_snapshot_event(
            store,
            lambda: store.commit(
                {"tampered": True},
                version=2,
                provided_checksum="0" * 64,
            ),
        )
    )
    snapshot_events.append(
        _catch_snapshot_event(
            store,
            lambda: store.commit(
                {"replacement": "old"},
                version=1,
                provided_checksum=None,
            ),
        )
    )

    def busy_probe() -> dict[str, Any]:
        with store.hold_commit_lock():
            return _catch_snapshot_event(
                store,
                lambda: store.commit(
                    {"replacement": "concurrent"},
                    version=99,
                ),
            )

    snapshot_events.append(busy_probe())

    scenarios: list[dict[str, Any]] = []

    def run_scenario(
        name: str,
        description: str,
        scenario_state: FlightState,
        version: int,
    ) -> FlightState:
        committed = store.commit(
            scenario_state.snapshot_payload(),
            version=version,
            provided_checksum=content_hash(scenario_state.snapshot_payload()),
        )
        scenario_state = _with_snapshot(
            scenario_state,
            committed.snapshot.version,
            committed.snapshot.checksum,
        )
        incremental = graph.build(scenario_state)
        full = graph.build(scenario_state, force_recompute=True)
        scenarios.append(
            _scenario_summary(name, description, incremental, full)
        )
        snapshot_events.append(
            {
                "status": committed.status,
                "version": committed.snapshot.version,
                "checksum": committed.snapshot.checksum,
                "code": None,
                "scenario": name,
            }
        )
        return scenario_state

    mag = base_state.descriptions["mag"]
    noise_state = base_state.replace_description(
        "mag",
        replace(
            mag,
            noise_model={
                **mag.noise_model,
                "white_noise_std": mag.noise_model.get("white_noise_std", 0.0) * 1.6,
            },
            provenance={**mag.provenance, "noise_model": "runtime.noise_adjustment"},
        ),
    )
    run_scenario(
        "noise_adjustment",
        "磁力计噪声模型调整，仅失效 mag 解析及其融合后缀",
        noise_state,
        2,
    )

    accel = base_state.descriptions["accel"]
    axis_state = base_state.replace_description(
        "accel",
        replace(
            accel,
            axis_order=("x", "y", "z"),
            provenance={**accel.provenance, "axis_order": "runtime.axis_correction"},
        ),
    )
    run_scenario(
        "axis_order_correction",
        "加速度计轴序修正为 x,y,z，复用其余传感器全部上游",
        axis_state,
        3,
    )

    gyro = base_state.descriptions["gyro"]
    calibration_state = base_state.replace_description(
        "gyro",
        gyro.with_calibration_version(4),
    )
    run_scenario(
        "calibration_upgrade",
        "陀螺仪标定版本 v3 -> v4，仅重算 gyro 后缀",
        calibration_state,
        4,
    )

    weight_config = replace(
        base_state.fusion_config,
        config_version="fusion-1.0.1",
        accel_weight=0.62,
    )
    weight_state = base_state.replace_config(weight_config)
    run_scenario(
        "weight_only",
        "仅调整加速度计融合权重；描述与 gyro/mag 贡献节点必须复用",
        weight_state,
        5,
    )

    injection_specs: list[Any] = [
        "parse.axis_missing@mag",
        "preprocess.calibration_rollback@gyro",
        "preprocess.sample_rate_nonpositive@accel",
        "fusion.gain_out_of_range",
        "output.residual_missing",
    ]
    if extra_injections:
        injection_specs.extend(extra_injections)
    injection_plan = InjectionPlan.from_specs(injection_specs)
    injection_build = graph.build(base_state, injection_plan=injection_plan)
    injection_full = graph.build(
        base_state,
        injection_plan=injection_plan,
        force_recompute=True,
    )

    migration = _migration_report(base_data)
    current = store.current
    history = [
        {
            "version": item.version,
            "checksum": item.checksum,
            "payload_digest": content_hash(item.payload),
        }
        for item in store.history
    ]
    report: dict[str, Any] = {
        "schema": "flightfusion.demo.v1",
        "seed": base_state.dataset.seed,
        "data": {
            "dataset_hash": base_state.dataset.content_hash(),
            "truth_hash": base_state.dataset.truth.content_hash(),
            "samples": {
                sensor_id: len(sequence.times)
                for sensor_id, sequence in sorted(base_state.dataset.sequences.items())
            },
        },
        "formats": dict(sorted(base_state.description_views.items())),
        "active_formats": dict(sorted(base_state.active_formats.items())),
        "initial": {
            **initial.to_dict(),
            "all_recomputed_on_first_pass": all(
                node.status == "recomputed" for node in initial.nodes.values()
            ),
            "second_build_cache_all_reused": (
                not repeat.recomputed_nodes and len(repeat.reused_nodes) == len(repeat.nodes)
            ),
            "forced_recompute_equal": initial.output_bytes() == initial_full.output_bytes(),
            "cache_repeat_equal": cache_repeat_equal,
        },
        "cache": {
            "directory": str(cache_path),
            "stats": graph.cache.stats(),
        },
        "migrations": migration,
        "incremental": scenarios,
        "injection": {
            "plan": injection_plan.as_dict(),
            "recomputed_nodes": injection_build.recomputed_nodes,
            "reused_nodes": injection_build.reused_nodes,
            "affected_sensors": injection_build.affected_sensors,
            "diagnostics": injection_build.diagnostics,
            "business_codes": injection_build.business_codes,
            "full_recompute_equal": (
                injection_build.output_bytes() == injection_full.output_bytes()
            ),
            "stats": injection_build.output["stats"],
        },
        "snapshot": {
            "active_version": current.version if current else None,
            "active_checksum": current.checksum if current else None,
            "history": history,
            "events": snapshot_events,
            "last_error_code": store.last_error_code,
            "whole_replacement": True,
        },
        "timeseries": {
            "times": initial.output["times"],
            "euler_deg": initial.output["euler_deg"],
            "truth_euler_deg": initial.output["truth_euler_deg"],
            "residual": initial.output["residual"],
            "accel_residual": initial.output["accel_residual"],
            "mag_residual": initial.output["mag_residual"],
        },
        "acceptance": {
            "three_format_interoperability": all(
                item["behavior_preserved"] for item in migration.values()
            ),
            "deterministic_preprocess_and_fusion": (
                initial.output_bytes() == initial_full.output_bytes()
                and initial.output_bytes() == repeat.output_bytes()
            ),
            "incremental_equals_full": all(
                item["full_recompute_equal"] for item in scenarios
            ),
            "weight_change_sensor_isolation": next(
                (
                    set(item["recomputed_nodes"])
                    == {"fusion:accel", "fusion:merge", "output:attitude"}
                    and set(item["reused_nodes"])
                    == {
                        "parse:accel",
                        "parse:gyro",
                        "parse:mag",
                        "preprocess:accel",
                        "preprocess:gyro",
                        "preprocess:mag",
                        "fusion:gyro",
                        "fusion:mag",
                    }
                    for item in scenarios
                    if item["name"] == "weight_only"
                ),
                False,
            ),
            "four_stage_injection_chain": {
                "parse",
                "preprocess",
                "fusion",
                "output",
            }.issubset({item.get("stage") for item in injection_build.diagnostics}),
            "injection_equals_full": (
                injection_build.output_bytes() == injection_full.output_bytes()
            ),
            "snapshot_codes_present": {
                "E_STAGE_BUSY",
                "E_SNAPSHOT_CORRUPT",
                "E_SNAPSHOT_ROLLBACK",
            }.issubset(
                {
                    item.get("code")
                    for item in snapshot_events
                    if item.get("code")
                }
            ),
        },
    }
    if artifact_path is not None:
        target = Path(artifact_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return report


def print_demo_summary(report: dict[str, Any]) -> None:
    initial = report["initial"]
    print(f"seed={report['seed']} samples={report['data']['samples']}")
    print(
        "initial nodes="
        f"{len(initial['nodes'])} cache_reuse={initial['second_build_cache_all_reused']}"
    )
    print(f"attitude stats={initial['output']['stats']}")
    print(f"acceptance={json.dumps(report['acceptance'], ensure_ascii=False)}")
    print("incremental:")
    for item in report["incremental"]:
        print(
            f"  {item['name']}: sensors={item['affected_sensors']} "
            f"recomputed={item['recomputed_nodes']} equal={item['full_recompute_equal']}"
        )
    print(f"injection codes={report['injection']['business_codes']}")
    print(
        "snapshot version="
        f"{report['snapshot']['active_version']} events="
        f"{[event.get('code') or event['status'] for event in report['snapshot']['events']]}"
    )

