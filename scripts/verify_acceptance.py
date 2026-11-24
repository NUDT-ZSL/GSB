from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from formbench.cli import main as cli_main
from formbench.detect import detect_blocks
from formbench.evaluation import match_to_truth
from formbench.generator import generate_dataset
from formbench.imaging import read_pgm
from formbench.runner import RunOptions, Workbench, serial_recognize
from formbench.schema import load_task, load_truth
from formbench.scheduler import WorkItem, plan_rounds


NORMAL_IDS = ["form-001", "form-002", "form-003"]


def spec_for(task, image_id):
    return next(item for item in task.images if item.image_id == image_id)


def budget_check() -> dict:
    items = [WorkItem(index=i, buffer_bytes=4096, logic_cost=2.5) for i in range(12)]
    reproducible = True
    plans = {}
    for concurrency in (1, 2, 4, 8):
        first = plan_rounds(
            items,
            concurrency=concurrency,
            memory_budget_bytes=12 * 1024,
            round_logic_budget=8.0,
        )
        second = plan_rounds(
            items,
            concurrency=concurrency,
            memory_budget_bytes=12 * 1024,
            round_logic_budget=8.0,
        )
        reproducible &= [r.indices for r in first.rounds] == [r.indices for r in second.rounds]
        reproducible &= first.admission_order == second.admission_order
        for round_plan in first.rounds:
            assert len(round_plan.indices) <= concurrency
            assert round_plan.projected_bytes <= 12 * 1024
        plans[str(concurrency)] = {
            "round_count": first.round_count,
            "admission_order": first.admission_order,
            "rounds": [
                {
                    "round": r.round_number,
                    "indices": r.indices,
                    "bytes": r.projected_bytes,
                    "logic": r.projected_logic,
                    "close_reason": r.close_reason,
                }
                for r in first.rounds
            ],
        }
    return {"reproducible": reproducible, "plans": plans}


def main() -> int:
    generate_dataset(ROOT)
    task_path = ROOT / "samples" / "task.json"
    task = load_task(task_path)
    workspace = Path(tempfile.mkdtemp(prefix="formbench-acceptance-"))

    detection_report = {}
    for image_id in NORMAL_IDS:
        spec = spec_for(task, image_id)
        image = read_pgm(spec.path)
        first = [b.to_dict() for b in detect_blocks(image, detection_threshold=task.detection_threshold)]
        second = [b.to_dict() for b in detect_blocks(image, detection_threshold=task.detection_threshold)]
        truth = load_truth(spec.truth_path)
        blocks = detect_blocks(image, detection_threshold=task.detection_threshold)
        matches = match_to_truth(blocks, truth)
        ious = [matches[i][1] for i in range(len(blocks))]
        detection_report[image_id] = {
            "block_count": len(blocks),
            "truth_count": len(truth),
            "repeatable": first == second,
            "min_iou": round(min(ious), 6),
            "mean_iou": round(sum(ious) / len(ious), 6),
        }

    parallel_state = workspace / "parallel-state"
    parallel_output = workspace / "parallel-output"
    with Workbench(
        task,
        options=RunOptions(state_dir=parallel_state, output_dir=parallel_output, concurrency=4),
    ) as bench:
        batch = bench.run()

    consistency = {}
    for image_id in NORMAL_IDS:
        spec = spec_for(task, image_id)
        serial = serial_recognize(task, spec)
        form = next(form for form in batch["forms"] if form["image_id"] == image_id)
        parallel = sorted(form["blocks"], key=lambda b: b["block_index"])
        same_text = [a["text"] for a in parallel] == [b["text"] for b in serial]
        same_conf = [a["confidence"] for a in parallel] == [b["confidence"] for b in serial]
        same_score = [a["score"] for a in parallel] == [b["score"] for b in serial]
        consistency[image_id] = {
            "serial_blocks": len(serial),
            "parallel_blocks": len(parallel),
            "text_equal": same_text,
            "confidence_equal": same_conf,
            "score_equal": same_score,
            "adjudication_passed": form["adjudication"]["passed"],
        }

    interrupt_state = workspace / "interrupt-state"
    interrupt_output = workspace / "interrupt-output"
    common_cli = [
        "run",
        "--task",
        str(task_path),
        "--state-dir",
        str(interrupt_state),
        "--output-dir",
        str(interrupt_output),
    ]
    first_code = cli_main([*common_cli, "--concurrency", "1", "--stop-after-blocks", "3"])
    marker = interrupt_state / "INTERRUPTED.json"
    marker_written = marker.exists()
    partial_blocks = len(
        list((interrupt_state / "images" / "form-001" / "blocks").glob("block-*.json"))
    )
    resume_code = cli_main(common_cli)
    resumed_form = json.loads((interrupt_output / "forms" / "form-001.json").read_text("utf-8"))
    resumed_diag = json.loads((interrupt_output / "diagnostics.json").read_text("utf-8"))
    form_diag = next(
        item for item in resumed_diag["diagnostics"] if item["image_id"] == "form-001"
    )
    interrupt_report = {
        "first_exit_code": first_code,
        "marker_written": marker_written,
        "partial_block_files": partial_blocks,
        "resume_exit_code": resume_code,
        "resume_mode": form_diag["recovery_mode"],
        "pending_after_resume_start": form_diag["pending_block_count"],
        "final_block_count": resumed_form["block_count"],
        "adjudication_passed": resumed_form["adjudication"]["passed"],
    }

    fault_report = {
        error["image_id"]: error["reason_code"] for error in batch["errors"]
    }
    report = {
        "ok": True,
        "task_id": task.task_id,
        "temporary_workspace": str(workspace),
        "detection": detection_report,
        "serial_parallel_consistency": consistency,
        "budget_scheduling": budget_check(),
        "interrupt_resume": interrupt_report,
        "fault_codes": fault_report,
        "batch": {
            "form_count": batch["form_count"],
            "error_count": batch["error_count"],
            "status": batch["status"],
        },
    }

    checks = [
        all(item["repeatable"] and item["block_count"] == 11 for item in detection_report.values()),
        all(
            item["text_equal"]
            and item["confidence_equal"]
            and item["score_equal"]
            and item["adjudication_passed"]
            for item in consistency.values()
        ),
        report["budget_scheduling"]["reproducible"],
        first_code == 130
        and marker_written
        and partial_blocks == 3
        and resume_code == 1
        and resumed_form["block_count"] == 11
        and resumed_form["adjudication"]["passed"],
        set(fault_report) == {"form-corrupt", "form-grid", "form-ledger", "form-vocab"},
    ]
    report["ok"] = all(checks)
    reports_dir = ROOT / "reports"
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / "acceptance-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(report_path)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
