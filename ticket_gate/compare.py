from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _normalize_batches(batches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "batch_no": batch["batch_no"],
            "gate_ids": batch["gate_ids"],
            "cycle_gate_ids": batch["cycle_gate_ids"],
        }
        for batch in batches
    ]


def _normalize_gate_versions(
    gate_versions: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    fields = (
        "gate_id",
        "state",
        "target_versions",
        "active_versions",
        "activated_batch",
        "attempt_count",
    )
    return [
        {field: gate[field] for field in fields}
        for gate in gate_versions
    ]


def _first_differences(
    expected: Any,
    actual: Any,
    path: str = "$",
    limit: int = 20,
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []

    def visit(left: Any, right: Any, current_path: str) -> None:
        if len(differences) >= limit:
            return
        if type(left) is not type(right):
            differences.append(
                {
                    "path": current_path,
                    "expected": left,
                    "actual": right,
                    "kind": "type",
                }
            )
            return
        if isinstance(left, dict):
            for key in sorted(set(left).union(right)):
                if key not in left:
                    differences.append(
                        {
                            "path": f"{current_path}.{key}",
                            "expected": "<missing>",
                            "actual": right[key],
                            "kind": "missing_expected",
                        }
                    )
                elif key not in right:
                    differences.append(
                        {
                            "path": f"{current_path}.{key}",
                            "expected": left[key],
                            "actual": "<missing>",
                            "kind": "missing_actual",
                        }
                    )
                else:
                    visit(left[key], right[key], f"{current_path}.{key}")
                if len(differences) >= limit:
                    return
            return
        if isinstance(left, list):
            if len(left) != len(right):
                differences.append(
                    {
                        "path": f"{current_path}.length",
                        "expected": len(left),
                        "actual": len(right),
                        "kind": "length",
                    }
                )
            for index, (left_item, right_item) in enumerate(zip(left, right)):
                visit(left_item, right_item, f"{current_path}[{index}]")
                if len(differences) >= limit:
                    return
            return
        if left != right:
            differences.append(
                {
                    "path": current_path,
                    "expected": left,
                    "actual": right,
                    "kind": "value",
                }
            )

    visit(expected, actual, path)
    return differences


def compare_documents(
    kernel_summary: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    kernel_plan = kernel_summary["propagation_plan"]
    sections = {
        "affected_gate_ids": (
            reference["affected_gate_ids"],
            kernel_summary["affected_gate_ids"],
        ),
        "dependencies": (
            reference["dependencies"],
            kernel_plan["dependencies"],
        ),
        "batches": (
            reference["batches"],
            _normalize_batches(kernel_plan["batches"]),
        ),
        "cycles": (
            reference["cycles"],
            kernel_plan["cycles"],
        ),
        "multi_subscribed_conflicting_gates": (
            reference["multi_subscribed_conflicting_gates"],
            kernel_plan["multi_subscribed_conflicting_gates"],
        ),
        "gate_versions": (
            reference["gate_versions"],
            _normalize_gate_versions(kernel_summary["gate_versions"]),
        ),
        "degraded_gates": (
            reference["degraded_gates"],
            kernel_summary["degradation"]["gates"],
        ),
        "decisions": (
            reference["decisions"],
            kernel_summary["verification"]["decisions"],
        ),
        "decision_stats": (
            reference["decision_stats"],
            kernel_summary["verification"]["stats"],
        ),
    }
    results: dict[str, Any] = {}
    all_ok = True
    for name, (expected, actual) in sections.items():
        differences = _first_differences(expected, actual)
        ok = not differences
        all_ok = all_ok and ok
        results[name] = {
            "ok": ok,
            "difference_count_at_least": len(differences),
            "differences": differences,
        }

    invariant_failures = {
        name: value
        for name, value in kernel_summary.get("invariants", {}).items()
        if value is not True
    }
    if invariant_failures:
        all_ok = False
    return {
        "ok": all_ok,
        "project_id": kernel_summary.get("project_id"),
        "reference_project_id": reference.get("project_id"),
        "sections": results,
        "kernel_invariant_failures": invariant_failures,
    }


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a kernel report with the independent reference judge"
    )
    parser.add_argument("--kernel", required=True, help="kernel report directory or summary.json")
    parser.add_argument("--reference", required=True, help="reference.json")
    parser.add_argument("--out", help="optional comparison JSON output path")
    args = parser.parse_args(argv)

    kernel_path = Path(args.kernel)
    if kernel_path.is_dir():
        kernel_path = kernel_path / "summary.json"
    result = compare_documents(load_json(kernel_path), load_json(args.reference))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(rendered)
            handle.write("\n")
    print(rendered)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
