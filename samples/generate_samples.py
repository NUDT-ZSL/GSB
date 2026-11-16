"""Generate deterministic sample DAG definition files locally.

Running this script recreates:
  samples/dags/pipeline_valid.json
  samples/dags/pipeline_failure.json
  samples/dags/pipeline_cycle.json
No network access or external service is required.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DAG_DIR = os.path.join(HERE, "dags")


def _stage(
    stage_id,
    script,
    deps,
    resource_units,
    estimated_ms,
    output,
    retries=0,
    timeout_ms=20000,
):
    return {
        "stage_id": stage_id,
        "script_path": f"../stages/{script}",
        "dependencies": deps,
        "resource_units": resource_units,
        "estimated_ms": estimated_ms,
        "retries": retries,
        "output_path": f"outputs/{stage_id}.jsonl",
        "timeout_ms": timeout_ms,
    }


def valid_dag() -> dict:
    return {
        "dag_id": "sample-pipeline",
        "dag_version": "1.0.0",
        "global_resource_budget": 4,
        "default_max_concurrency": 2,
        "default_timeout_ms": 20000,
        "stages": [
            _stage("ingest", "stage_ingest.py", [], 2, 120, "ingest"),
            _stage(
                "enrich",
                "stage_enrich.py",
                [{"stage_id": "ingest", "kind": "strong"}],
                2,
                90,
                "enrich",
            ),
            _stage(
                "validate",
                "stage_validate.py",
                [{"stage_id": "ingest", "kind": "weak"}],
                1,
                70,
                "validate",
            ),
            _stage(
                "aggregate",
                "stage_aggregate.py",
                [{"stage_id": "enrich", "kind": "strong"}],
                3,
                110,
                "aggregate",
            ),
            _stage(
                "report",
                "stage_report.py",
                [
                    {"stage_id": "aggregate", "kind": "strong"},
                    {"stage_id": "validate", "kind": "weak"},
                ],
                1,
                60,
                "report",
            ),
        ],
    }


def failure_dag() -> dict:
    dag = valid_dag()
    dag["dag_id"] = "sample-pipeline-failure"
    dag["dag_version"] = "1.0.0-failure"
    for stage in dag["stages"]:
        if stage["stage_id"] == "validate":
            stage["retries"] = 2
        if stage["stage_id"] == "aggregate":
            stage["retries"] = 1
    return dag


def cycle_dag() -> dict:
    dag = valid_dag()
    dag["dag_id"] = "sample-pipeline-cycle"
    dag["dag_version"] = "1.0.0-cycle"
    for stage in dag["stages"]:
        if stage["stage_id"] == "ingest":
            stage["dependencies"] = [{"stage_id": "report", "kind": "strong"}]
    return dag


def tight_budget_dag() -> dict:
    """Two independent slow 2-unit roots with budget 3 force deterministic waiting."""
    return {
        "dag_id": "sample-pipeline-tight",
        "dag_version": "1.0.0-tight",
        "global_resource_budget": 3,
        "default_max_concurrency": 2,
        "default_timeout_ms": 20000,
        "stages": [
            _stage(
                "root_a",
                "stage_slow_ingest.py",
                [],
                2,
                400,
                "root_a",
            ),
            _stage(
                "root_b",
                "stage_slow_ingest.py",
                [],
                2,
                400,
                "root_b",
            ),
            _stage(
                "join",
                "stage_report.py",
                [
                    {"stage_id": "root_a", "kind": "strong"},
                    {"stage_id": "root_b", "kind": "strong"},
                ],
                1,
                60,
                "join",
            ),
        ],
    }


def write_dag(name: str, payload: dict) -> str:
    os.makedirs(DAG_DIR, exist_ok=True)
    path = os.path.join(DAG_DIR, name)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def main() -> None:
    paths = [
        write_dag("pipeline_valid.json", valid_dag()),
        write_dag("pipeline_failure.json", failure_dag()),
        write_dag("pipeline_cycle.json", cycle_dag()),
        write_dag("pipeline_tight_budget.json", tight_budget_dag()),
    ]
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
