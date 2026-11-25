#!/usr/bin/env python3
"""Run the deterministic acceptance workflow and emit a structured report."""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from dedup_workbench.checkpoint import ForcedTermination  # noqa: E402
from dedup_workbench.engine import DedupWorkbench  # noqa: E402
from dedup_workbench.errors import WorkbenchError  # noqa: E402
from dedup_workbench.ingest import content_hash_for  # noqa: E402
from dedup_workbench.query import QueryService  # noqa: E402


SAMPLE_INPUT = PROJECT_ROOT / "samples" / "corpus_shards.jsonl"
SAMPLE_CONFIG = PROJECT_ROOT / "samples" / "dedup_config.json"
REPORT_DIR = PROJECT_ROOT / "reports"
RUNTIME_ROOT = REPORT_DIR / "acceptance_runtime"


def load_sample_config() -> dict[str, Any]:
    return json.loads(SAMPLE_CONFIG.read_text(encoding="utf-8"))


def summarize_run(report: dict[str, Any]) -> dict[str, Any]:
    incremental = report["incremental"]
    return {
        "document_count": report["stats"]["document_count"],
        "shard_count": report["stats"]["shard_count"],
        "block_count": report["stats"]["block_count"],
        "candidate_pair_count": report["candidates"]["pair_count"],
        "qualifying_pair_count": report["decisions"]["qualifying_pair_count"],
        "removed_count": len(report["decisions"]["removed_doc_ids"]),
        "removed_doc_ids": report["decisions"]["removed_doc_ids"],
        "self_check_count": report["self_check"]["check_count"],
        "incremental": {
            "recomputed_docs": incremental["recomputed_docs"],
            "reused_docs_count": len(incremental["reused_docs"]),
            "signature_recomputed_docs": incremental["signature_recomputed_docs"],
            "comparison_recomputed_pairs_count": len(incremental["comparison_recomputed_pairs"]),
            "comparison_reused_pairs_count": len(incremental["comparison_reused_pairs"]),
            "decision_recomputed_components": incremental["decision_recomputed_components"],
            "decision_reused_components_count": len(incremental["decision_reused_components"]),
            "recomputed_nodes": incremental["recomputed_nodes"],
            "disjoint": incremental["disjoint"],
            "covers_all_docs": incremental["covers_all_docs"],
        },
        "cache_stats": report["stats"]["cache"],
    }


def run_baseline(runtime: Path) -> tuple[DedupWorkbench, dict[str, Any]]:
    workbench = DedupWorkbench(runtime, SAMPLE_CONFIG)
    return workbench, workbench.run(SAMPLE_INPUT)


def revised_content_input(target_doc_id: str, destination: Path) -> Path:
    records = [json.loads(line) for line in SAMPLE_INPUT.read_text(encoding="utf-8").splitlines() if line.strip()]
    for record in records:
        if record["doc_id"] == target_doc_id:
            record["text"] = record["text"] + " incremental acceptance revision block"
            record["content_hash"] = content_hash_for(record["text"])
    destination.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records) + "\n",
        encoding="utf-8",
    )
    return destination


def run_pytest_suite() -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    lines = [line for line in output.splitlines() if line.strip()]
    return {
        "returncode": completed.returncode,
        "passed": completed.returncode == 0,
        "summary": lines[-1] if lines else "",
        "tail": "\n".join(lines[-8:]),
    }


def main() -> int:
    if RUNTIME_ROOT.exists():
        shutil.rmtree(RUNTIME_ROOT)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    generator = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_sample.py")],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    generator_info = json.loads(generator.stdout.strip().splitlines()[-1])

    baseline_dir = RUNTIME_ROOT / "baseline"
    baseline_workbench, baseline_report = run_baseline(baseline_dir)
    baseline_summary = summarize_run(baseline_report)

    query_service = QueryService(baseline_dir / "exports" / "doc_records.jsonl", load_sample_config())
    query_checks = []
    all_identical = True
    for doc_id in query_service.doc_ids():
        query = query_service.query(doc_id, iterations=11)
        all_identical = all_identical and query["byte_identical_to_batch_line"]
        query_checks.append(
            {
                "doc_id": doc_id,
                "byte_identical": query["byte_identical_to_batch_line"],
                "record_sha256": query["record_sha256"],
                "observed_avg_ms": query["timing"]["observed_avg_ms"],
                "estimated_ms": query["timing"]["estimated_ms"],
                "comparison_count": query["timing"]["comparison_count"],
                "signature_block_count": query["timing"]["signature_block_count"],
            }
        )

    try:
        query_service.query("doc-0001", budget_ms=0.00000001, iterations=5)
        latency_probe = {"code": "UNEXPECTED_SUCCESS", "passed": False}
    except WorkbenchError as exc:
        latency_probe = {"code": exc.code, "field": exc.field, "passed": exc.code == "E_LATENCY_BUDGET", "details": exc.details}

    identical_rerun = DedupWorkbench(baseline_dir, SAMPLE_CONFIG).run(SAMPLE_INPUT)

    scenarios: dict[str, Any] = {"identical_rerun": summarize_run(identical_rerun)}
    scenario_specs = {
        "threshold_0.95": {"similarity_threshold": 0.95},
        "validation_digest": {"validation_mode": "digest"},
        "window_1": {"candidate_window": 1},
        "chunk_length_24": {"chunk_signature_length": 24},
    }
    base_config = load_sample_config()
    for name, changes in scenario_specs.items():
        scenario_dir = RUNTIME_ROOT / name
        _baseline, scenario_baseline = run_baseline(scenario_dir / "root")
        changed_config = json.loads(json.dumps(base_config))
        changed_config.update(changes)
        changed_config["config_version"] = int(base_config["config_version"]) + 1
        changed_report = DedupWorkbench(scenario_dir / "root", changed_config).run(SAMPLE_INPUT)
        scenarios[name] = {
            "baseline": summarize_run(scenario_baseline),
            "changed": summarize_run(changed_report),
        }

    unique_doc = next(
        doc_id
        for doc_id, decision in baseline_report["decisions"]["decisions"].items()
        if decision["status"] == "unique" and len(decision["threshold_checks"]) == 0
    )
    revision_dir = RUNTIME_ROOT / "content_revision"
    _revision_baseline_wb, revision_baseline = run_baseline(revision_dir / "root")
    revised_input = revised_content_input(unique_doc, revision_dir / "revised_corpus.jsonl")
    revision_report = DedupWorkbench(revision_dir / "root", SAMPLE_CONFIG).run(revised_input)
    scenarios["content_revision"] = {
        "target_doc_id": unique_doc,
        "baseline": summarize_run(revision_baseline),
        "changed": summarize_run(revision_report),
    }

    recovery_dir = RUNTIME_ROOT / "recovery"
    recovery_wb = DedupWorkbench(recovery_dir, SAMPLE_CONFIG)
    forced_termination_observed = False
    try:
        recovery_wb.run(SAMPLE_INPUT, crash_after_batch=1)
    except ForcedTermination:
        forced_termination_observed = True
    checkpoint_after_crash = json.loads((recovery_dir / "checkpoints" / "batch_executor.json").read_text(encoding="utf-8"))
    recovered_report = DedupWorkbench(recovery_dir, SAMPLE_CONFIG).run(SAMPLE_INPUT)

    pytest_result = run_pytest_suite()
    report = {
        "ok": all_identical and latency_probe["passed"] and pytest_result["passed"] and recovered_report["self_check"]["ok"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "pytest": pytest.__version__,
        },
        "sample_generator": generator_info,
        "baseline": baseline_summary,
        "query_consistency": {
            "checked_doc_count": len(query_checks),
            "all_byte_identical_to_batch_records": all_identical,
            "queries": query_checks,
        },
        "latency_budget_probe": latency_probe,
        "incremental_scenarios": scenarios,
        "recovery": {
            "forced_termination_observed": forced_termination_observed,
            "committed_after_crash": checkpoint_after_crash["committed_batch_indexes"],
            "resumed": recovered_report["checkpoint"],
            "self_check_ok": recovered_report["self_check"]["ok"],
        },
        "pytest": pytest_result,
        "artifacts": {
            "sample_input": str(SAMPLE_INPUT),
            "sample_config": str(SAMPLE_CONFIG),
            "runtime_root": str(RUNTIME_ROOT),
            "batch_doc_records": str(baseline_dir / "exports" / "doc_records.jsonl"),
        },
    }

    report_path = REPORT_DIR / "acceptance_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    markdown = render_markdown(report)
    markdown_path = REPORT_DIR / "ACCEPTANCE_REPORT.md"
    markdown_path.write_text(markdown, encoding="utf-8")

    print(json.dumps({key: report[key] for key in ("ok", "baseline", "query_consistency", "pytest", "recovery") if key in report}, ensure_ascii=False, indent=2, sort_keys=True))
    print(f"\nStructured report: {report_path}")
    print(f"Markdown report: {markdown_path}")
    return 0 if report["ok"] else 1


def render_markdown(report: dict[str, Any]) -> str:
    baseline = report["baseline"]
    lines = [
        "# Acceptance Verification Report",
        "",
        f"- Generated at (UTC): `{report['generated_at_utc']}`",
        f"- Python: `{report['environment']['python']}`",
        f"- NumPy: `{report['environment']['numpy']}`",
        f"- pytest: `{report['environment']['pytest']}`",
        f"- Overall status: `{'PASS' if report['ok'] else 'FAIL'}`",
        "",
        "## Deterministic Sample",
        "",
        f"- Records: `{report['sample_generator']['record_count']}`",
        f"- Shards: `{baseline['shard_count']}`",
        f"- Signature blocks: `{baseline['block_count']}`",
        f"- Candidate pairs: `{baseline['candidate_pair_count']}`",
        f"- Qualifying near-duplicate pairs: `{baseline['qualifying_pair_count']}`",
        f"- Removed docs: `{baseline['removed_count']}`",
        f"- Removed IDs: `{', '.join(baseline['removed_doc_ids'])}`",
        "",
        "## Query Consistency",
        "",
        f"- Checked docs: `{report['query_consistency']['checked_doc_count']}`",
        f"- All query records byte-identical to batch export: `{report['query_consistency']['all_byte_identical_to_batch_records']}`",
        f"- Latency budget probe: `{report['latency_budget_probe']['code']}`",
        "",
        "## Incremental Propagation",
        "",
        "| Scenario | Recomputed docs | Reused docs | Comparison recompute/reuse | Recomputed nodes |",
        "|---|---:|---:|---:|---|",
    ]
    for name, scenario in report["incremental_scenarios"].items():
        changed = scenario.get("changed", scenario)
        inc = changed["incremental"]
        lines.append(
            f"| `{name}` | {len(inc['recomputed_docs'])} | {inc['reused_docs_count']} | "
            f"{inc['comparison_recomputed_pairs_count']}/{inc['comparison_reused_pairs_count']} | "
            f"`{', '.join(inc['recomputed_nodes']) or 'none'}` |"
        )
    recovery = report["recovery"]
    lines.extend(
        [
            "",
            "## Recovery",
            "",
            f"- Forced termination after committed batches: observed=`{recovery['forced_termination_observed']}`",
            f"- Committed after crash: `{recovery['committed_after_crash']}`",
            f"- Skipped on resume: `{recovery['resumed']['skipped_committed_batches']}`",
            f"- Executed on resume: `{recovery['resumed']['executed_batches']}`",
            "",
            "## pytest",
            "",
            f"- Status: `{'PASS' if report['pytest']['passed'] else 'FAIL'}`",
            f"- Summary: `{report['pytest']['summary']}`",
            "",
            "```text",
            report["pytest"]["tail"],
            "```",
            "",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
