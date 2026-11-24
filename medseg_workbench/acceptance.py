from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any, Dict

import numpy as np

from .evaluation import build_evaluation_report, export_mask, export_report
from .io_utils import atomic_write_json
from .project import INTERRUPT_EXIT_CODE, Project
from .sample_data import generate_sample


def run_acceptance(out_root: Path | str = Path("artifacts/acceptance")) -> Dict[str, Any]:
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    sample_root = out_root / "sample"
    generate_sample(sample_root, force=True)
    project = Project.from_task(sample_root / "task.json").prepare()
    first = project.run()
    initial_mask = project.assemble_mask()
    _, initial_full = project.full_reference()
    initial_exact = bool(np.array_equal(initial_mask, initial_full))

    revision = project.demo_revision("001_001_001")
    invalidation = project.invalidation_plan()
    incremental = project.run()
    incremental_mask = project.assemble_mask()
    _, incremental_full = project.full_reference()
    incremental_exact = bool(np.array_equal(incremental_mask, incremental_full))

    final_revision = project.demo_final_revision("001_001_001")
    final_invalidation = project.invalidation_plan()
    final_incremental = project.run()
    final_mask = project.assemble_mask()
    _, final_full = project.full_reference()
    final_exact = bool(np.array_equal(final_mask, final_full))

    reuse_run = project.run()
    evaluation = build_evaluation_report(project)
    mask_export = export_mask(project, out_root / "mask.npz")
    report_export = export_report(evaluation, out_root / "report.json")

    recovery_root = out_root / "recovery-sample"
    generate_sample(recovery_root, force=True)
    recovery_project = Project.from_task(recovery_root / "task.json").prepare()
    stop_event = threading.Event()
    stop_event.set()
    interrupted = recovery_project.run(stop_event=stop_event)
    interrupt_ok = bool(interrupted.get("interrupted") and interrupted.get("exit_code") == INTERRUPT_EXIT_CODE)
    resumed = recovery_project.run()
    recovery_mask = recovery_project.assemble_mask()
    _, recovery_full = recovery_project.full_reference()
    recovery_exact = bool(np.array_equal(recovery_mask, recovery_full))

    result: Dict[str, Any] = {
        "ok": all(
            [
                initial_exact,
                incremental_exact,
                final_exact,
                recovery_exact,
                interrupt_ok,
                reuse_run["completed_this_run"] == [],
            ]
        ),
        "initial": {
            "exact_vs_full_serial": initial_exact,
            "chunks": project.cfg.chunk_count,
            "rounds": first["plan"]["round_count"],
            "admission_sequence_preview": first["plan"]["admission_sequence"][:8],
        },
        "revision": {
            "chunk": revision.chunk_id,
            "digest": revision.digest(),
            "impacted_chunks": invalidation["invalid_chunk_ids"],
            "stale_chunks": invalidation["stale_chunk_ids"],
            "reason_counts": invalidation["reason_counts"],
        },
        "incremental": {
            "exact_vs_full_with_same_revisions": incremental_exact,
            "recomputed": incremental["completed_this_run"],
            "reused_count": incremental["reused_count"],
            "second_run_completed": reuse_run["completed_this_run"],
            "second_run_reused": len(reuse_run["reused_chunks"]),
        },
        "final_mask_revision": {
            "chunk": final_revision.chunk_id,
            "stale_chunks": final_invalidation["stale_chunk_ids"],
            "recomputed": final_incremental["completed_this_run"],
            "exact_vs_full_with_same_edits": final_exact,
        },
        "recovery": {
            "interrupt_exit_code": INTERRUPT_EXIT_CODE,
            "interrupt_marker_written": interrupt_ok,
            "resume_completed": len(resumed["completed_this_run"]),
            "resume_reused": len(resumed["reused_chunks"]),
            "exact_vs_full_serial": recovery_exact,
        },
        "evaluation": {
            "dice": evaluation["overlap"]["dice"],
            "iou": evaluation["overlap"]["iou"],
            "precision": evaluation["overlap"]["precision"],
            "recall": evaluation["overlap"]["recall"],
            "invalid_count": evaluation["invalidation"]["invalid_count"],
        },
        "seams": {"seam_count": project.seam_report()["seam_count"]},
        "export": {"mask": mask_export, "report": report_export},
    }
    atomic_write_json(out_root / "acceptance-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local deterministic acceptance checks")
    parser.add_argument("--out", type=Path, default=Path("artifacts/acceptance"))
    args = parser.parse_args()
    result = run_acceptance(args.out)
    print(__import__("json").dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
