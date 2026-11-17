from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENTS = ROOT / "experiments"


def main() -> int:
    _assert_writable(ROOT)
    if EXPERIMENTS.exists():
        shutil.rmtree(EXPERIMENTS)
    EXPERIMENTS.mkdir(parents=True)

    experiments = [
        {
            "name": "exp1_single_partition",
            "partitions": 1,
            "scale_factor": 100,
            "directory": "exp1_single_partition",
        },
        {
            "name": "exp2_multi_partition",
            "partitions": 4,
            "scale_factor": 100,
            "directory": "exp2_multi_partition",
        },
        {
            "name": "exp3_scale_perturbation",
            "partitions": 4,
            "scale_factor": 10000,
            "directory": "exp3_scale_perturbation",
        },
    ]
    injections = [
        {"name": "nullmap", "inject": "nullmap", "expected_code": "E_NULLMAP", "directory": "injections/nullmap"},
        {"name": "scalef", "inject": "scalef", "expected_code": "E_SCALEF", "directory": "injections/scalef"},
        {"name": "part", "inject": "part", "expected_code": "E_PART", "directory": "injections/part"},
    ]

    reports: dict[str, dict[str, Any]] = {}
    for experiment in experiments:
        output = EXPERIMENTS / experiment["directory"]
        command = [
            sys.executable,
            str(ROOT / "run.py"),
            "run",
            "--tables",
            "data/sample_tables.json",
            "--query",
            "data/sample_query.json",
            "--partitions",
            str(experiment["partitions"]),
            "--scale-factor",
            str(experiment["scale_factor"]),
            "--output",
            str(output),
            "--quiet",
        ]
        _run(command, expected_exit=0)
        reports[experiment["name"]] = json.loads((output / "report.json").read_text(encoding="utf-8"))

    injection_results: list[dict[str, Any]] = []
    for injection in injections:
        output = EXPERIMENTS / injection["directory"]
        command = [
            sys.executable,
            str(ROOT / "run.py"),
            "run",
            "--tables",
            "data/sample_tables.json",
            "--query",
            "data/sample_query.json",
            "--inject",
            injection["inject"],
            "--output",
            str(output),
            "--quiet",
        ]
        _run(command, expected_exit=2)
        error_payload = json.loads((output / "error.json").read_text(encoding="utf-8"))
        if error_payload.get("code") != injection["expected_code"]:
            raise RuntimeError(
                f"injection {injection['name']} returned {error_payload.get('code')}, "
                f"expected {injection['expected_code']}"
            )
        injection_results.append(
            {
                "name": injection["name"],
                "expected_code": injection["expected_code"],
                "actual_code": error_payload["code"],
                "table": error_payload.get("table"),
                "row": error_payload.get("row"),
                "column": error_payload.get("column"),
                "error_report": f"{injection['directory']}/error.json",
            }
        )

    manifest = {
        "schema_version": "1.0",
        "experiments": [
            {
                "name": experiment["name"],
                "report": f"{experiment['directory']}/report.json",
            }
            for experiment in experiments
        ],
        "injections": [
            {
                "name": injection["name"],
                "expected_code": injection["expected_code"],
                "error_report": f"{injection['directory']}/error.json",
            }
            for injection in injections
        ],
    }
    (EXPERIMENTS / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    verify_command = [
        sys.executable,
        str(ROOT / "tools" / "verify_reports.py"),
        "--tables",
        "data/sample_tables.json",
        "--query",
        "data/sample_query.json",
        "--manifest",
        str(EXPERIMENTS / "manifest.json"),
        "--output",
        str(EXPERIMENTS / "verification.json"),
        "--quiet",
    ]
    _run(verify_command, expected_exit=0)
    verification = json.loads((EXPERIMENTS / "verification.json").read_text(encoding="utf-8"))

    summary = _build_summary(reports, injection_results, verification)
    (EXPERIMENTS / "demo_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (EXPERIMENTS / "demo_summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    print(json.dumps({"status": "OK", "summary": str(EXPERIMENTS / "demo_summary.md")}, ensure_ascii=False, sort_keys=True))
    return 0


def _build_summary(
    reports: dict[str, dict[str, Any]],
    injection_results: list[dict[str, Any]],
    verification: dict[str, Any],
) -> dict[str, Any]:
    experiments: list[dict[str, Any]] = []
    for name, report in reports.items():
        attribution_counts: dict[str, int] = {"quantization": 0, "aggregation": 0, "both": 0}
        for row in report["rows"]:
            attribution_counts[row["error_attribution"]["primary_source"]] += 1
        experiments.append(
            {
                "name": name,
                "partitions": report["config"]["partitions"],
                "scale_factor": report["config"]["scale_factor"],
                "rows": len(report["rows"]),
                "partition_global_digest": report["shuffle"]["global_digest"],
                "output_digest": report["determinism"]["output_digest"],
                "quantized_reference_collision_count": report["join"]["quantized_reference_collision_count"],
                "aggregate": {
                    "count_total": report["aggregate"]["count_total"],
                    "count_non_null": report["aggregate"]["count_non_null"],
                    "count_null": report["aggregate"]["count_null"],
                    "sum_fixed_lattice": report["aggregate"]["sum_fixed_lattice"],
                    "sum_error_bound_lattice": report["aggregate"]["sum_error_bound_lattice"],
                    "mean_fixed_lattice": report["aggregate"]["mean_fixed_lattice"],
                    "mean_error_bound_lattice": report["aggregate"]["mean_error_bound_lattice"],
                },
                "profile": [
                    {
                        "partition": item["partition"],
                        "left_rows": item["left_input_rows"],
                        "right_rows": item["right_input_rows"],
                        "bucket_capacity": item["bucket_capacity"],
                        "load_factor": item["load_factor"],
                        "bucket_probe_count": item["bucket_probe_count"],
                        "overflow_probe_count": item["overflow_probe_count"],
                        "matched_pair_count": item["matched_pair_count"],
                    }
                    for item in report["join"]["partition_statistics"]
                ],
                "segments": report["error_propagation"]["segments"],
                "dominant_segment": report["error_propagation"]["dominant_segment"],
                "row_attribution_counts": attribution_counts,
            }
        )
    return {
        "schema_version": "1.0",
        "status": "OK",
        "experiments": experiments,
        "injections": injection_results,
        "verification": {
            "status": verification["status"],
            "checked_reports": verification["checked_reports"],
            "injection_checks": len(verification["injection_checks"]),
            "output": "verification.json",
        },
    }


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Fixed-point Shuffle Hash Join Demo",
        "",
        "All values below were produced from local deterministic JSON files. No network or account access is used.",
        "",
        "## Experiments",
        "",
        "| Experiment | Partitions | Scale | Rows | Collisions | Sum lattice | Sum bound | Mean lattice | Mean bound | Dominant |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in summary["experiments"]:
        aggregate = item["aggregate"]
        lines.append(
            f"| {item['name']} | {item['partitions']} | {item['scale_factor']} | {item['rows']} | "
            f"{item['quantized_reference_collision_count']} | {aggregate['sum_fixed_lattice']} | "
            f"{aggregate['sum_error_bound_lattice']} | {aggregate['mean_fixed_lattice']} | "
            f"{aggregate['mean_error_bound_lattice']} | {item['dominant_segment']} |"
        )
    lines.extend(["", "## Partition Profiles", ""])
    for item in summary["experiments"]:
        lines.append(f"### {item['name']}")
        lines.append("")
        lines.append("| Partition | Left | Right | Capacity | Load | Probes | Overflow probes | Matches |")
        lines.append("| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for profile in item["profile"]:
            lines.append(
                f"| {profile['partition']} | {profile['left_rows']} | {profile['right_rows']} | "
                f"{profile['bucket_capacity']} | {profile['load_factor']} | {profile['bucket_probe_count']} | "
                f"{profile['overflow_probe_count']} | {profile['matched_pair_count']} |"
            )
        lines.extend(["", "Segments:", ""])
        for segment in item["segments"]:
            lines.append(
                f"- `{segment['segment']}`: bound `{segment['bound_lattice']}` lattice units, "
                f"affected rows `{segment['affected_rows']}/{segment['denominator_rows']}` "
                f"({segment['affected_row_ratio']['percent']}%)."
            )
        counts = item["row_attribution_counts"]
        lines.extend(
            [
                "",
                f"Row attribution counts: quantization `{counts['quantization']}`, aggregation `{counts['aggregation']}`, both `{counts['both']}`.",
                "",
            ]
        )
    lines.extend(["## Injected Failures", "", "| Injection | Code | Table | Row | Column |", "| --- | --- | --- | ---: | --- |"])
    for item in summary["injections"]:
        lines.append(
            f"| {item['name']} | {item['actual_code']} | {item['table'] or ''} | {item['row'] or ''} | {item['column'] or ''} |"
        )
    lines.extend(
        [
            "",
            "## Verification",
            "",
            f"Independent verification status: `{summary['verification']['status']}`. "
            f"Checked reports: `{summary['verification']['checked_reports']}`; "
            f"injection checks: `{summary['verification']['injection_checks']}`.",
            "",
            "The verifier recomputed partition digests, output digests, fixed aggregates, attribution, error bounds, and dominant segment selection.",
            "",
        ]
    )
    return "\n".join(lines)


def _assert_writable(path: Path) -> None:
    probe = path / ".demo_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(f"working directory is not writable: {exc}") from exc


def _run(command: list[str], expected_exit: int) -> None:
    completed = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if completed.returncode != expected_exit:
        raise RuntimeError(
            f"command failed with exit {completed.returncode}, expected {expected_exit}: {' '.join(command)}\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
