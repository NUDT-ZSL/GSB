from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .diagnostics import diagnose_plan
from .errors import EngineError
from .model import Scenario
from .planner import PlanResult, Planner
from .preflight import PreflightReport, preflight_unit
from .replay import ReplayResult, replay_scenario
from .scenario import load_scenario, load_scenario_parts

DEFAULT_DIAGNOSTIC_THRESHOLD = 1e-12


@dataclass(frozen=True)
class Execution:
    scenario: Scenario
    primary_plans: dict[str, PlanResult]
    preflights: dict[str, PreflightReport]
    selected_plans: dict[str, PlanResult]
    replay: ReplayResult
    diagnostics: dict[str, dict[str, object]]

    def summary(self) -> dict[str, object]:
        return {
            "ok": True,
            "scenario_id": self.scenario.scenario_id,
            "version": self.scenario.fingerprint,
            "replay": dict(self.replay.summary),
            "plans": {
                unit_id: {
                    "final_path": [list(point) for point in plan.path],
                    "path_cost_fixed": plan.cost.fixed_total,
                    "reference_cost": plan.cost.reference_total,
                    "error_bound": plan.cost.error_bound,
                    "cost_within_tolerance": plan.cost.within_tolerance,
                    "budget_decision": plan.budget.to_dict(),
                    "possibly_non_optimal": plan.possibly_non_optimal,
                    "actual_cost_ratio": plan.actual_cost_ratio,
                }
                for unit_id, plan in self.primary_plans.items()
            },
            "preflight": {
                unit_id: {
                    "selected_candidate_id": report.selected_candidate_id,
                    "selected_path": [list(point) for point in report.selected_path],
                    "merge_order": list(report.merge_order),
                    "peak_buffer_bytes": report.peak_buffer_bytes,
                }
                for unit_id, report in self.preflights.items()
            },
        }


def execute_scenario(
    scenario: Scenario,
    *,
    resume_snapshot: dict[str, Any] | None = None,
    diagnostic_threshold: float = DEFAULT_DIAGNOSTIC_THRESHOLD,
) -> Execution:
    primary_plans: dict[str, PlanResult] = {}
    preflights: dict[str, PreflightReport] = {}
    selected_plans: dict[str, PlanResult] = {}
    planner = Planner(scenario)
    for unit in scenario.units:
        primary = planner.plan_unit(unit)
        preflight = preflight_unit(scenario, unit)
        selected = primary
        if preflight.selected_candidate_id is not None:
            selected_result = next(
                result
                for result in preflight.results
                if result.candidate_id == preflight.selected_candidate_id
            )
            selected = selected_result.plan
        primary_plans[unit.id] = primary
        preflights[unit.id] = preflight
        selected_plans[unit.id] = selected
    replay = replay_scenario(scenario, resume_snapshot)
    diagnostics = {
        unit_id: {
            "primary": diagnose_plan(primary_plans[unit_id], diagnostic_threshold),
            "preflight_selected": diagnose_plan(
                selected_plans[unit_id],
                diagnostic_threshold,
            ),
        }
        for unit_id in primary_plans
    }
    return Execution(
        scenario=scenario,
        primary_plans=primary_plans,
        preflights=preflights,
        selected_plans=selected_plans,
        replay=replay,
        diagnostics=diagnostics,
    )


def _safe_reset_directory(path: Path, protected_paths: list[Path]) -> None:
    resolved = path.resolve()
    if len(resolved.parts) < 3 or resolved == Path.home().resolve():
        raise EngineError(
            "E_OUTPUT_PATH",
            "refusing to clear an unsafe output path",
            {"path": str(resolved)},
        )
    for protected in protected_paths:
        protected_resolved = protected.resolve()
        if resolved == protected_resolved or resolved in protected_resolved.parents:
            raise EngineError(
                "E_OUTPUT_PATH",
                "output/state directory must not contain the input scenario",
                {"path": str(resolved), "protected": str(protected_resolved)},
            )
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_snapshot_file(path: str | Path) -> dict[str, Any]:
    snapshot_path = Path(path)
    try:
        value = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EngineError(
            "E_SNAPSHOT_IO",
            "unable to read snapshot file",
            {"path": str(snapshot_path), "reason": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise EngineError(
            "E_SNAPSHOT_FORMAT",
            "snapshot file is not valid JSON",
            {"path": str(snapshot_path), "reason": str(exc)},
        ) from exc
    if not isinstance(value, dict):
        raise EngineError(
            "E_SNAPSHOT_FORMAT",
            "snapshot root must be an object",
            {"path": str(snapshot_path)},
        )
    return value


def write_execution(
    execution: Execution,
    output_directory: str | Path,
    state_directory: str | Path,
) -> dict[str, object]:
    output_path = Path(output_directory)
    state_path = Path(state_directory)
    output_path.mkdir(parents=True, exist_ok=True)
    state_path.mkdir(parents=True, exist_ok=True)

    plan_payload = {
        unit_id: {
            "primary": execution.primary_plans[unit_id].to_dict(),
            "selected": execution.selected_plans[unit_id].to_dict(),
        }
        for unit_id in (unit.id for unit in execution.scenario.units)
    }
    preflight_payload = {
        unit_id: report.to_dict()
        for unit_id, report in execution.preflights.items()
    }
    replay_payload = execution.replay.to_dict()
    diagnostics_payload = {
        "threshold_default": DEFAULT_DIAGNOSTIC_THRESHOLD,
        "units": execution.diagnostics,
    }
    summary_payload = execution.summary()

    _atomic_write_json(output_path / "plan.json", plan_payload)
    _atomic_write_json(output_path / "preflight.json", preflight_payload)
    _atomic_write_json(output_path / "replay.json", replay_payload)
    _atomic_write_json(output_path / "diagnostics.json", diagnostics_payload)
    _atomic_write_json(output_path / "summary.json", summary_payload)

    snapshot_paths: list[Path] = []
    for snapshot in execution.replay.snapshots:
        event_index = int(snapshot["event_index"])
        snapshot_path = state_path / f"snapshot_{event_index:03d}.json"
        _atomic_write_json(snapshot_path, snapshot)
        snapshot_paths.append(snapshot_path)
    if execution.replay.snapshots:
        _atomic_write_json(
            state_path / "current_snapshot.json",
            execution.replay.snapshots[-1],
        )

    manifest_files = [
        output_path / "plan.json",
        output_path / "preflight.json",
        output_path / "replay.json",
        output_path / "diagnostics.json",
        output_path / "summary.json",
        *snapshot_paths,
    ]
    if execution.replay.snapshots:
        manifest_files.append(state_path / "current_snapshot.json")
    try:
        project_root = Path.cwd()
        manifest_path_text = {
            path: str(path.resolve().relative_to(project_root.resolve()))
            for path in manifest_files
        }
    except ValueError:
        manifest_path_text = {path: str(path.resolve()) for path in manifest_files}
    manifest = {
        "scenario_id": execution.scenario.scenario_id,
        "version": execution.scenario.fingerprint,
        "replay_digest": execution.replay.digest,
        "files": [
            {
                "path": manifest_path_text[path],
                "sha256": _sha256_file(path),
            }
            for path in manifest_files
        ],
    }
    _atomic_write_json(output_path / "manifest.json", manifest)
    return summary_payload


def run_scenario_files(
    scenario_path: str | Path,
    output_directory: str | Path = "output",
    state_directory: str | Path = "state",
    *,
    resume_snapshot_path: str | Path | None = None,
    diagnostic_threshold: float = DEFAULT_DIAGNOSTIC_THRESHOLD,
) -> dict[str, object]:
    scenario_file = Path(scenario_path)
    return _run_with_loader(
        lambda: load_scenario(scenario_file),
        [scenario_file],
        output_directory,
        state_directory,
        resume_snapshot_path,
        diagnostic_threshold,
    )


def run_scenario_parts(
    terrain_path: str | Path,
    units_path: str | Path,
    replay_path: str | Path,
    options_path: str | Path,
    output_directory: str | Path = "output",
    state_directory: str | Path = "state",
    *,
    resume_snapshot_path: str | Path | None = None,
    diagnostic_threshold: float = DEFAULT_DIAGNOSTIC_THRESHOLD,
) -> dict[str, object]:
    input_paths = [
        Path(terrain_path),
        Path(units_path),
        Path(replay_path),
        Path(options_path),
    ]
    return _run_with_loader(
        lambda: load_scenario_parts(
            terrain_path,
            units_path,
            replay_path,
            options_path,
        ),
        input_paths,
        output_directory,
        state_directory,
        resume_snapshot_path,
        diagnostic_threshold,
    )


def _run_with_loader(
    loader: Any,
    input_paths: list[Path],
    output_directory: str | Path,
    state_directory: str | Path,
    resume_snapshot_path: str | Path | None,
    diagnostic_threshold: float,
) -> dict[str, object]:
    output_path = Path(output_directory)
    state_path = Path(state_directory)
    if output_path.resolve() == state_path.resolve():
        raise EngineError(
            "E_OUTPUT_PATH",
            "output and state directories must be distinct",
            {"path": str(output_path.resolve())},
        )
    resume_snapshot = (
        load_snapshot_file(resume_snapshot_path)
        if resume_snapshot_path is not None
        else None
    )
    cwd = Path.cwd()
    protected_paths = [*input_paths, cwd]
    _safe_reset_directory(output_path, protected_paths)
    _safe_reset_directory(state_path, protected_paths)
    scenario = loader()
    execution = execute_scenario(
        scenario,
        resume_snapshot=resume_snapshot,
        diagnostic_threshold=diagnostic_threshold,
    )
    return write_execution(execution, output_directory, state_directory)
