"""End-to-end deterministic demo and acceptance evidence generator."""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .engine import Engine, RunOutcome
from .errors import BusinessError, E_CONFIG_MISMATCH
from .graph import validate_graph
from .injections import load_injections
from .reports import write_report
from .runtime import StopController
from .state import StateStore
from .util import deep_copy_json, load_json


EXPECTED_EDIT_RESULTS = {
    "EDIT-001": {
        "invalidated": ["conv1", "interp_bi", "conv2"],
        "reused": ["conv3", "interp_nn"],
        "removed": [],
    },
    "EDIT-002": {
        "invalidated": ["interp_bi", "conv2"],
        "reused": ["conv1", "conv3", "interp_nn"],
        "removed": [],
    },
    "EDIT-003": {
        "invalidated": ["conv3b", "interp_nn_b"],
        "reused": ["conv1", "interp_bi", "conv2"],
        "removed": ["conv3", "interp_nn"],
    },
    "EDIT-004": {
        "invalidated": [],
        "reused": ["conv1", "conv3b", "interp_bi", "interp_nn_b"],
        "removed": ["conv2"],
    },
    "EDIT-005": {
        "invalidated": ["interp_extra"],
        "reused": ["conv1", "conv3b", "interp_bi", "interp_nn_b"],
        "removed": [],
    },
}


def _clear_directory(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _load_samples(project_root: Path) -> dict[str, Any]:
    samples = project_root / "samples"
    return {
        "graph": load_json(samples / "graph.json"),
        "input": load_json(samples / "input.json"),
        "config": load_json(samples / "config.json"),
        "edits": load_json(samples / "edits.json"),
        "injections_path": samples / "injections.json",
    }


def _initialize(
    root: Path,
    *,
    graph_raw: dict[str, Any],
    descriptor_raw: dict[str, Any],
    input_array: np.ndarray,
    config_raw: dict[str, Any],
    graph_version: int = 0,
) -> StateStore:
    store = StateStore(root)
    store.initialize(
        graph_raw=graph_raw,
        config_raw=config_raw,
        input_descriptor_raw=descriptor_raw,
        input_array=input_array,
        graph_version=graph_version,
    )
    return store


def _fresh_full(
    root: Path,
    *,
    graph_raw: dict[str, Any],
    descriptor_raw: dict[str, Any],
    input_array: np.ndarray,
    config_raw: dict[str, Any],
    graph_version: int,
) -> tuple[StateStore, RunOutcome]:
    store = _initialize(
        root,
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config_raw,
        graph_version=graph_version,
    )
    outcome = Engine(store, config_raw).run(store.load())
    return store, outcome


def _compare_products(reference: StateStore, candidate: StateStore) -> dict[str, Any]:
    reference_snapshot = reference.load()
    candidate_snapshot = candidate.load()
    validation = validate_graph(reference_snapshot["graph"])
    if validation.graph is None:
        raise RuntimeError("reference graph is invalid")
    graph = validation.graph
    node_checks: list[dict[str, Any]] = []
    equivalent = True
    for node_id in graph.topo_order:
        reference_record = reference_snapshot.get("node_products", {}).get(node_id)
        candidate_record = candidate_snapshot.get("node_products", {}).get(node_id)
        if reference_record is None or candidate_record is None:
            node_checks.append(
                {
                    "node_id": node_id,
                    "present_both": False,
                    "equivalent": False,
                }
            )
            equivalent = False
            continue
        reference_array = reference.load_product(reference_record)
        candidate_array = candidate.load_product(candidate_record)
        arrays_equal = (
            reference_array is not None
            and candidate_array is not None
            and reference_array.shape == candidate_array.shape
            and np.array_equal(reference_array, candidate_array, equal_nan=True)
        )
        fingerprints_equal = (
            reference_record.get("tensor_fingerprint") == candidate_record.get("tensor_fingerprint")
        )
        summaries_equal = reference_record.get("summary") == candidate_record.get("summary")
        cache_keys_equal = reference_record.get("cache_key") == candidate_record.get("cache_key")
        node_equal = arrays_equal and fingerprints_equal and summaries_equal and cache_keys_equal
        equivalent = equivalent and node_equal
        node_checks.append(
            {
                "node_id": node_id,
                "present_both": True,
                "equivalent": node_equal,
                "elementwise_equal": arrays_equal,
                "fingerprints_equal": fingerprints_equal,
                "summaries_equal": summaries_equal,
                "cache_keys_equal": cache_keys_equal,
                "tensor_fingerprint": reference_record.get("tensor_fingerprint"),
            }
        )
    outputs_equal = reference_snapshot.get("final_outputs") == candidate_snapshot.get("final_outputs")
    failures_equal = reference_snapshot.get("failures", []) == candidate_snapshot.get("failures", [])
    return {
        "equivalent": equivalent and outputs_equal and failures_equal,
        "nodes": node_checks,
        "final_outputs_equal": outputs_equal,
        "failures_equal": failures_equal,
    }


def _report_summary(report: dict[str, Any], path: Path) -> dict[str, Any]:
    return {
        "report": str(path),
        "status": report.get("status"),
        "exit_code": report.get("exit_code"),
        "graph_version": report.get("graph_version"),
        "recalculated_nodes": report.get("recalculated_nodes", []),
        "reused_nodes": report.get("reused_nodes", []),
        "invalidated_nodes": report.get("invalidated_nodes", []),
        "removed_nodes": report.get("removed_nodes", []),
        "business_codes": report.get("business_codes", []),
    }


def _fault_check(
    output: Path,
    *,
    sample_graph: dict[str, Any],
    sample_input: dict[str, Any],
    sample_config: dict[str, Any],
    input_array: np.ndarray,
    injections_path: Path,
    injection_id: str,
    expected_codes: list[str],
    expected_success_nodes: list[str],
) -> dict[str, Any]:
    root = output / "faults" / injection_id / "state"
    store = _initialize(
        root,
        graph_raw=sample_graph,
        descriptor_raw=sample_input,
        input_array=input_array,
        config_raw=sample_config,
    )
    injections = load_injections(injections_path, enable_ids=[injection_id])
    outcome = Engine(store, sample_config, injections).run(store.load())
    report_path = output / "faults" / injection_id / "report.json"
    write_report(report_path, outcome.report)
    status_by_node = {node["node_id"]: node["status"] for node in outcome.report["nodes"]}
    successful = [node_id for node_id, status in status_by_node.items() if status in {"recalculated", "reused"}]
    hit_ids = [hit["id"] for hit in outcome.report.get("injections_hit", [])]
    failure_injection_ids = [failure.get("injection_id") for failure in outcome.report.get("failures", [])]
    checks = {
        "codes_present": all(code in outcome.report.get("business_codes", []) for code in expected_codes),
        "successful_independent_nodes": all(node_id in successful for node_id in expected_success_nodes),
        "injection_proven": injection_id in hit_ids or injection_id in failure_injection_ids,
    }
    return {
        "injection_id": injection_id,
        "status": outcome.report["status"],
        "business_codes": outcome.report.get("business_codes", []),
        "expected_codes": expected_codes,
        "node_status": status_by_node,
        "successful_nodes": successful,
        "expected_success_nodes": expected_success_nodes,
        "injections_hit": outcome.report.get("injections_hit", []),
        "failures": outcome.report.get("failures", []),
        "checks": checks,
        "passed": all(checks.values()),
        **_report_summary(outcome.report, report_path),
    }


def run_demo(output: Path, *, project_root: Path) -> int:
    output = output.resolve()
    project_root = project_root.resolve()
    if output == project_root or output == Path("/"):
        raise BusinessError(E_CONFIG_MISMATCH, "refusing to use project root as demo output", stage="demo")
    _clear_directory(output)
    samples = _load_samples(project_root)
    config = samples["config"]
    graph_raw = samples["graph"]
    descriptor_raw = samples["input"]
    validation = validate_graph(graph_raw)
    if validation.graph is None:
        raise RuntimeError("sample graph invalid")
    from .inputs import generate_input, load_input_descriptor

    generated = generate_input(
        load_input_descriptor(project_root / "samples" / "input.json"),
        validation.graph,
    )
    input_array = generated.tensor

    verification: dict[str, Any] = {
        "verification_version": 1,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "platform": platform.platform(),
        },
        "samples": {
            "graph": str(project_root / "samples" / "graph.json"),
            "input": str(project_root / "samples" / "input.json"),
            "config": str(project_root / "samples" / "config.json"),
            "edits": str(project_root / "samples" / "edits.json"),
            "injections": str(samples["injections_path"]),
        },
    }

    full_store = _initialize(
        output / "full" / "state",
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
    )
    full_outcome = Engine(full_store, config).run(full_store.load())
    full_report_path = output / "full" / "report.json"
    write_report(full_report_path, full_outcome.report)
    verification["full_run"] = {
        **_report_summary(full_outcome.report, full_report_path),
        "passed": full_outcome.report["status"] == "complete"
        and len(full_outcome.report["recalculated_nodes"]) == 5
        and not full_outcome.report["reused_nodes"],
    }

    incremental_store = _initialize(
        output / "incremental" / "state",
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
    )
    initial_incremental = Engine(incremental_store, config).run(incremental_store.load())
    initial_report_path = output / "incremental" / "reports" / "graph_v0000.json"
    write_report(initial_report_path, initial_incremental.report)
    incremental_checks: list[dict[str, Any]] = []
    operations = samples["edits"]["operations"]
    current_snapshot = initial_incremental.snapshot
    for operation in operations:
        engine = Engine(incremental_store, config)
        commit = engine.commit_edit(current_snapshot, operation)
        if not commit.committed:
            raise RuntimeError(f"edit failed: {operation.get('id')}: {commit.failures}")
        outcome = engine.run(commit.snapshot)
        current_snapshot = outcome.snapshot
        report_path = output / "incremental" / "reports" / f"graph_v{outcome.report['graph_version']:04d}.json"
        outcome.report["edit"] = commit.edit_info
        outcome.report["edit_commit"] = {
            "invalidated_nodes": commit.invalidated_nodes,
            "reused_nodes": commit.reused_nodes,
            "removed_nodes": commit.removed_nodes,
            "invalidation_chains": commit.invalidation_chains,
        }
        write_report(report_path, outcome.report)
        baseline_root = output / "equivalence" / f"graph_v{outcome.report['graph_version']:04d}" / "state"
        baseline_store, baseline_outcome = _fresh_full(
            baseline_root,
            graph_raw=deep_copy_json(current_snapshot["graph"]),
            descriptor_raw=descriptor_raw,
            input_array=input_array,
            config_raw=config,
            graph_version=int(current_snapshot["graph_version"]),
        )
        comparison = _compare_products(incremental_store, baseline_store)
        edit_id = commit.edit_info["id"]
        expected = EXPECTED_EDIT_RESULTS.get(edit_id, {})
        non_executed_resused_ok = all(
            node_id not in outcome.report.get("execution_trace", [])
            for node_id in outcome.report.get("reused_nodes", [])
        )
        invalidation_ok = (
            commit.invalidated_nodes == expected.get("invalidated", commit.invalidated_nodes)
            and commit.reused_nodes == expected.get("reused", commit.reused_nodes)
            and commit.removed_nodes == expected.get("removed", commit.removed_nodes)
        )
        alignment_metadata = None
        if edit_id in {"EDIT-002", "EDIT-003", "EDIT-005"}:
            node_id = "interp_bi" if edit_id == "EDIT-002" else ("interp_nn_b" if edit_id == "EDIT-003" else "interp_extra")
            record = current_snapshot.get("node_products", {}).get(node_id, {})
            metadata = record.get("operator_metadata", {})
            alignment_metadata = {
                "node_id": node_id,
                "alignment": metadata.get("alignment"),
                "rounding_rule": metadata.get("rounding_rule"),
                "boundary": metadata.get("boundary"),
            }
        check = {
            "edit_id": edit_id,
            "description": commit.edit_info.get("description", ""),
            "graph_version": outcome.report["graph_version"],
            "invalidated_nodes": commit.invalidated_nodes,
            "reused_nodes": commit.reused_nodes,
            "removed_nodes": commit.removed_nodes,
            "invalidation_chains": commit.invalidation_chains,
            "recalculated_nodes": outcome.report.get("recalculated_nodes", []),
            "executed_nodes": outcome.report.get("execution_trace", []),
            "reused_not_executed": non_executed_resused_ok,
            "invalidation_expectation_met": invalidation_ok,
            "equivalence": comparison,
            "alignment_metadata": alignment_metadata,
            "report": str(report_path),
        }
        check["passed"] = (
            outcome.report["status"] == "complete"
            and comparison["equivalent"]
            and non_executed_resused_ok
            and invalidation_ok
        )
        incremental_checks.append(check)
    verification["incremental"] = {
        "initial_report": str(initial_report_path),
        "edits": incremental_checks,
        "passed": bool(incremental_checks) and all(item["passed"] for item in incremental_checks),
    }

    final_graph_raw = deep_copy_json(current_snapshot["graph"])
    final_graph_version = int(current_snapshot["graph_version"])
    serial_config = deep_copy_json(config)
    serial_config["max_workers"] = 1
    parallel_config = deep_copy_json(config)
    parallel_config["max_workers"] = 4
    serial_store, serial_outcome = _fresh_full(
        output / "concurrency" / "serial" / "state",
        graph_raw=final_graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=serial_config,
        graph_version=final_graph_version,
    )
    parallel_store, parallel_outcome = _fresh_full(
        output / "concurrency" / "parallel" / "state",
        graph_raw=final_graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=parallel_config,
        graph_version=final_graph_version,
    )
    serial_report_path = output / "concurrency" / "serial" / "report.json"
    parallel_report_path = output / "concurrency" / "parallel" / "report.json"
    write_report(serial_report_path, serial_outcome.report)
    write_report(parallel_report_path, parallel_outcome.report)
    concurrency_comparison = _compare_products(serial_store, parallel_store)
    list_order_equal = (
        serial_outcome.report["recalculated_nodes"] == parallel_outcome.report["recalculated_nodes"]
        and serial_outcome.report["reused_nodes"] == parallel_outcome.report["reused_nodes"]
        and serial_outcome.report["failures"] == parallel_outcome.report["failures"]
        and [item["node_id"] for item in serial_outcome.report["nodes"]]
        == [item["node_id"] for item in parallel_outcome.report["nodes"]]
    )
    verification["concurrency"] = {
        "serial_report": str(serial_report_path),
        "parallel_report": str(parallel_report_path),
        "comparison": concurrency_comparison,
        "list_order_equal": list_order_equal,
        "passed": concurrency_comparison["equivalent"] and list_order_equal,
    }

    graceful_root = output / "recovery_graceful" / "state"
    graceful_store = _initialize(
        graceful_root,
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
    )
    graceful_injections = load_injections(
        samples["injections_path"], enable_ids=["INJ-INT-GRACEFUL"]
    )
    graceful_controller = StopController()
    with graceful_controller.installed_handlers():
        graceful_outcome = Engine(graceful_store, config, graceful_injections).run(
            graceful_store.load(), stop_controller=graceful_controller
        )
    graceful_interrupted_path = output / "recovery_graceful" / "interrupted.json"
    write_report(graceful_interrupted_path, graceful_outcome.report)
    graceful_resume = Engine(graceful_store, config).run(graceful_store.load())
    graceful_resumed_path = output / "recovery_graceful" / "resumed.json"
    write_report(graceful_resumed_path, graceful_resume.report)
    graceful_baseline_store, graceful_baseline = _fresh_full(
        output / "recovery_graceful" / "baseline" / "state",
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
        graph_version=0,
    )
    graceful_comparison = _compare_products(graceful_baseline_store, graceful_store)
    graceful_checks = {
        "interrupted_status": graceful_outcome.report["status"] == "interrupted",
        "interrupt_exit_zero": graceful_outcome.report["exit_code"] == 0,
        "interrupt_hit": any(
            item.get("id") == "INJ-INT-GRACEFUL"
            for item in graceful_outcome.report.get("injections_hit", [])
        ),
        "resume_complete": graceful_resume.report["status"] == "complete",
        "resume_reused_completed": len(graceful_resume.report["reused_nodes"]) == 4,
        "resume_recalculated_one": graceful_resume.report["recalculated_nodes"] == ["conv2"],
        "equivalent": graceful_comparison["equivalent"],
    }
    verification["recovery_graceful"] = {
        "interrupted_report": str(graceful_interrupted_path),
        "resumed_report": str(graceful_resumed_path),
        "interrupted_status": graceful_outcome.report["status"],
        "interrupted_completed_nodes": graceful_outcome.report.get("completed_nodes", []),
        "resume_reused_nodes": graceful_resume.report["reused_nodes"],
        "resume_recalculated_nodes": graceful_resume.report["recalculated_nodes"],
        "injections_hit": graceful_outcome.report.get("injections_hit", []),
        "comparison": graceful_comparison,
        "checks": graceful_checks,
        "passed": all(graceful_checks.values()),
    }

    try:
        StateStore(graceful_root).verify_resume(
            graceful_store.load(), config_raw={**config, "max_workers": 2}
        )
        config_mismatch_hit = False
        mismatch_code = None
    except BusinessError as exc:
        config_mismatch_hit = exc.code == E_CONFIG_MISMATCH
        mismatch_code = exc.code
    verification["config_mismatch"] = {
        "expected_code": E_CONFIG_MISMATCH,
        "actual_code": mismatch_code,
        "passed": config_mismatch_hit,
    }

    hard_store = _initialize(
        output / "recovery_hard" / "state",
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
    )
    hard_run_report = output / "recovery_hard" / "hard_run_should_not_exist.json"
    command = [
        sys.executable,
        "-m",
        "convedit",
        "run",
        "--state",
        str(hard_store.root),
        "--report",
        str(hard_run_report),
        "--injections",
        str(samples["injections_path"]),
        "--enable-injection",
        "INJ-KILL-HARD",
    ]
    process = subprocess.run(command, cwd=project_root, capture_output=True, text=True)
    hard_snapshot = hard_store.load()
    hard_resume_report = output / "recovery_hard" / "resumed.json"
    resume_process = subprocess.run(
        [
            sys.executable,
            "-m",
            "convedit",
            "resume",
            "--state",
            str(hard_store.root),
            "--report",
            str(hard_resume_report),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    hard_resumed_snapshot = hard_store.load()
    hard_baseline_store, hard_baseline = _fresh_full(
        output / "recovery_hard" / "baseline" / "state",
        graph_raw=graph_raw,
        descriptor_raw=descriptor_raw,
        input_array=input_array,
        config_raw=config,
        graph_version=0,
    )
    hard_comparison = _compare_products(hard_baseline_store, hard_store)
    hard_report = load_json(hard_resume_report) if hard_resume_report.is_file() else {}
    hard_checks = {
        "forced_kill_returncode": process.returncode == -9,
        "run_report_absent": not hard_run_report.is_file(),
        "partial_snapshot_atomic": bool(hard_snapshot.get("completed_nodes"))
        and not hard_run_report.is_file(),
        "resume_exit_zero": resume_process.returncode == 0,
        "resume_complete": hard_report.get("status") == "complete",
        "resume_reused_completed": len(hard_report.get("reused_nodes", [])) == 4,
        "resume_recalculated_one": hard_report.get("recalculated_nodes") == ["conv2"],
        "injection_id_retained": any(
            item.get("id") == "INJ-KILL-HARD"
            for item in hard_report.get("injections_hit", [])
        ),
        "equivalent": hard_comparison["equivalent"],
    }
    verification["recovery_hard"] = {
        "hard_kill_returncode": process.returncode,
        "hard_kill_stderr_tail": process.stderr[-500:],
        "partial_completed_nodes": hard_snapshot.get("completed_nodes", []),
        "resume_report": str(hard_resume_report),
        "resume_stdout": resume_process.stdout.strip(),
        "resume_status": hard_report.get("status"),
        "resume_reused_nodes": hard_report.get("reused_nodes", []),
        "resume_recalculated_nodes": hard_report.get("recalculated_nodes", []),
        "comparison": hard_comparison,
        "checks": hard_checks,
        "passed": all(hard_checks.values()),
    }

    fault_specs = [
        (
            "INJ-SHAPE-001",
            ["E_SHAPE_MISMATCH", "E_DEPENDENCY_FAILED"],
            ["conv3", "interp_nn"],
        ),
        (
            "INJ-PAD-001",
            ["E_PARAM_OUT_OF_RANGE", "E_DEPENDENCY_FAILED"],
            ["conv3", "interp_nn"],
        ),
        (
            "INJ-SCALE-001",
            ["E_PARAM_OUT_OF_RANGE", "E_DEPENDENCY_FAILED"],
            ["conv1", "conv3", "interp_nn"],
        ),
        (
            "INJ-NONFINITE-001",
            ["E_NONFINITE_INPUT", "E_DEPENDENCY_FAILED"],
            [],
        ),
    ]
    fault_results = []
    for injection_id, codes, successful_nodes in fault_specs:
        fault_results.append(
            _fault_check(
                output,
                sample_graph=graph_raw,
                sample_input=descriptor_raw,
                sample_config=config,
                input_array=input_array,
                injections_path=samples["injections_path"],
                injection_id=injection_id,
                expected_codes=codes,
                expected_success_nodes=successful_nodes,
            )
        )
    verification["fault_injection"] = {
        "scenarios": fault_results,
        "passed": all(item["passed"] for item in fault_results),
    }

    verification["passed"] = all(
        [
            verification["full_run"]["passed"],
            verification["incremental"]["passed"],
            verification["concurrency"]["passed"],
            verification["recovery_graceful"]["passed"],
            verification["recovery_hard"]["passed"],
            verification["config_mismatch"]["passed"],
            verification["fault_injection"]["passed"],
        ]
    )
    verification_path = output / "verification.json"
    write_report(verification_path, verification)
    summary_lines = [
        "# convedit verification",
        "",
        f"- Overall: **{'PASS' if verification['passed'] else 'FAIL'}**",
        f"- Python: `{verification['environment']['python']}`, NumPy: `{verification['environment']['numpy']}`",
        f"- Full run: `{verification['full_run']['status']}`",
        f"- Incremental edits: `{len(incremental_checks)}`, all equivalent: `{verification['incremental']['passed']}`",
        f"- Serial/concurrent equivalence: `{verification['concurrency']['passed']}`",
        f"- Graceful recovery: `{verification['recovery_graceful']['passed']}`",
        f"- SIGKILL recovery: `{verification['recovery_hard']['passed']}` (return code `{process.returncode}`)",
        f"- Fault isolation scenarios: `{len(fault_results)}`, all passed: `{verification['fault_injection']['passed']}`",
        f"- Config mismatch code: `{verification['config_mismatch']['actual_code']}`",
        "",
        "Detailed machine-readable evidence: `verification.json`.",
    ]
    (output / "SUMMARY.md").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")
    print(json.dumps({k: verification[k] for k in verification if k.endswith("passed") or k == "passed"}, ensure_ascii=False, sort_keys=True))
    print(str(verification_path))
    return 0 if verification["passed"] else 2
