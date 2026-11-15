"""Repeatable end-to-end demonstration, runnable without a display.

Run::

    python -m piano_workbench.demo --output-dir demo_output

The script generates a deterministic synthetic piano clip, applies a fixed
operation sequence, exercises undo/redo, simulates a forced termination and
restart, demonstrates every business error code, and writes a state-tree
snapshot plus an audio metrics report for local review.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from .controller import Controller, UNDO_LIMIT
from .errors import WorkbenchError
from .metrics import node_metrics
from .synth import synthesize_piano, write_wav
from .workspace import Workspace
from .constants import SAMPLE_RATE


def _safe(call):
    """Invoke a callable expected to fail; return its canonical JSON error."""

    try:
        call()
    except WorkbenchError as exc:
        return exc.to_json_dict()
    raise AssertionError("expected WorkbenchError was not raised")


def _metrics_line(label: str, pcm: np.ndarray) -> dict:
    metrics = node_metrics(pcm, SAMPLE_RATE)
    metrics["node_label"] = label
    return metrics


def run_demo(output_dir: str | Path) -> dict:
    output_dir = Path(output_dir).resolve()
    # The demo owns this directory: reset it so every run is reproducible.
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report: dict = {"steps": [], "metrics": [], "errors": {}, "recovery": {}}

    # 1) Deterministic synthetic piano clip -------------------------------- #
    clip_path = output_dir / "piano_synth_48k_mono.wav"
    pcm_root = synthesize_piano(duration_seconds=2.0, midi_note=60)
    write_wav(clip_path, pcm_root)
    report["source"] = {"path": str(clip_path), "samples": int(pcm_root.size),
                        "sha256_source": None}

    # 2) Fresh workspace from the clip ------------------------------------- #
    ws_dir = output_dir / "workspace"
    controller = Controller.from_wav(clip_path, ws_dir)
    root_id = controller.workspace.root_id
    report["root_id"] = root_id

    # 3) Fixed operation sequence ------------------------------------------ #
    n = pcm_root.size
    op_denoise = controller.apply_operation(
        "denoise", 0, n // 4, {"seed": 20240517}
    )
    op_envelope = controller.apply_operation(
        "envelope", n // 4, n // 2, {"gain_start": 1.0, "gain_end": 0.4}
    )
    op_fade = controller.apply_operation(
        "fade", n // 2, 3 * n // 4, {"mode": "out", "curve": "equal_power"}
    )
    op_patch = controller.apply_operation(
        "patch", 3 * n // 4, n - 1024, {"gl_iterations": 32}
    )
    applied = [op_denoise, op_envelope, op_fade, op_patch]
    report["steps"].append(
        {"phase": "apply", "operations": [node["id"] for node in applied]}
    )

    # Stacked operation on an identical selection is allowed.
    stacked = controller.apply_operation(
        "envelope", n // 4, n // 2, {"gain_start": 0.9, "gain_end": 0.9}
    )
    report["steps"].append({"phase": "stack_same_selection", "node": stacked["id"]})

    # 4) Determinism: rebuild every node twice and compare byte output ----- #
    determinism = []
    for node in controller.workspace.nodes.values():
        first = controller.render_node(node["id"])
        second = controller.render_node(node["id"])
        determinism.append(
            {"node_id": node["id"], "byte_identical": bool(np.array_equal(first, second))}
        )
        if not np.array_equal(first, second):
            raise AssertionError(f"non-deterministic rebuild at {node['id']}")
    report["determinism"] = determinism

    # 5) Metrics snapshot for the current head and each operation ---------- #
    head_after_apply = controller.current_node()["id"]
    for node in [controller.workspace.nodes[root_id], *applied, stacked]:
        report["metrics"].append(
            _metrics_line(node["op"], controller.render_node(node["id"]))
        )

    # 6) Undo / redo consistency ------------------------------------------- #
    head_pcm = controller.render_current()
    controller.undo()
    controller.undo()
    undone_id = controller.current_node()["id"]
    undone_pcm = controller.render_current()
    cached_undone = controller.workspace.node_pcm(undone_id)
    undo_matches_cache = bool(np.array_equal(undone_pcm, cached_undone))

    controller.redo()
    controller.redo()
    redone_id = controller.current_node()["id"]
    redone_pcm = controller.render_current()
    redo_roundtrip = bool(
        redone_id == head_after_apply and np.array_equal(redone_pcm, head_pcm)
    )
    report["steps"].append({
        "phase": "undo_redo",
        "undone_id": undone_id,
        "undo_matches_cache": undo_matches_cache,
        "redone_id": redone_id,
        "roundtrip_byte_identical": redo_roundtrip,
    })
    if not (undo_matches_cache and redo_roundtrip):
        raise AssertionError("undo/redo consistency check failed")

    # 7) New operation truncates the redo branch --------------------------- #
    # Step back once so the redo stack is non-empty, then apply a new op.
    controller.undo()
    before_redo_depth = len(controller.workspace.redo_stack)
    branch_node = controller.apply_operation(
        "fade", n - 1024, n - 512, {"mode": "in", "curve": "linear"}
    )
    report["steps"].append({
        "phase": "redo_truncation",
        "redo_depth_before": before_redo_depth,
        "redo_depth_after": len(controller.workspace.redo_stack),
        "branch_node": branch_node["id"],
        "truncated": len(controller.workspace.redo_stack) == 0,
    })

    # 8) Branch switch back to the earlier head ---------------------------- #
    controller.checkout_node(head_after_apply)
    branch_switch_pcm = controller.render_current()
    report["steps"].append({
        "phase": "branch_switch",
        "current_id": controller.current_node()["id"],
        "matches_original_head": bool(np.array_equal(branch_switch_pcm, head_pcm)),
        "branch_node_retained": branch_node["id"] in controller.workspace.nodes,
    })

    # 9) Controlled shutdown, then reopen ---------------------------------- #
    controller.workspace.close()
    reopened = Workspace.open(ws_dir)
    reopen_pcm = reopened.node_pcm(head_after_apply)
    report["recovery"]["controlled_reopen_match"] = bool(
        np.array_equal(reopen_pcm, head_pcm)
    )
    report["recovery"]["exit_marker"] = reopened.last_recovery.get("previous_exit")

    # 10) Forced termination simulation ------------------------------------ #
    # A forced kill cannot finish the commit: stage an orphan cache file that
    # no manifest references, write an honest forced-exit marker, then reopen.
    orphan_pcm = synthesize_piano(duration_seconds=0.5, midi_note=48)
    orphan_hash = reopened._write_cache(orphan_pcm)
    reopened.write_exit_marker(
        "forced",
        {"reason": "simulated_sigkill", "uncommitted_hash": orphan_hash},
    )
    recovered = Workspace.open(ws_dir)
    orphan_quarantined = not recovered.cache_path(orphan_hash).exists()
    orphan_kept = (recovered.orphan_dir / f"{orphan_hash}.wav").exists()
    stable_pcm = recovered.node_pcm(head_after_apply)
    report["recovery"].update({
        "forced_exit": True,
        "reclaimed_orphans": recovered.last_recovery["reclaimed_orphans"],
        "orphan_quarantined": orphan_quarantined and orphan_kept,
        "restored_current_id": recovered.current_id,
        "stable_state_byte_identical": bool(np.array_equal(stable_pcm, head_pcm)),
        "verified_caches": recovered.last_recovery["verified_caches"],
    })
    if not (orphan_quarantined and orphan_kept and
            np.array_equal(stable_pcm, head_pcm)):
        raise AssertionError("forced-termination recovery check failed")

    # 11) Error-code coverage ---------------------------------------------- #
    controller = Controller(recovered)
    report["errors"]["E_SEL_RANGE_bad_range"] = _safe(
        lambda: controller.apply_operation("fade", 100, 50)
    )
    report["errors"]["E_SEL_RANGE_overlap"] = _safe(
        lambda: controller.apply_operation("fade", 1000, 3000)
    )
    report["errors"]["E_NODE_MISSING"] = _safe(
        lambda: controller.render_node("n_does_not_exist")
    )
    report["errors"]["E_CACHE_HASH"] = _safe(
        lambda: recovered.read_cache("0" * 64)
    )
    report["errors"]["E_VERSION_CONFLICT"] = _safe(
        lambda: Workspace.open(ws_dir, expected_version=999)
    )
    report["errors"]["E_PARAM"] = _safe(
        lambda: controller.apply_operation(
            "envelope", n - 512, n - 256, {"gain_start": None}
        )
    )
    for entry in report["errors"].values():
        if entry["ok"] is not False or not entry["code"].startswith("E_"):
            raise AssertionError("error contract violation")

    # 12) Export head WAV and write the report ----------------------------- #
    export_path = output_dir / "head_export.wav"
    recovered.export_wav(recovered.current_id, export_path)
    report["export"] = {"path": str(export_path)}
    report["undo_limit"] = UNDO_LIMIT

    snapshot_path = output_dir / "state_snapshot.json"
    snapshot_path.write_text(
        json.dumps(recovered.snapshot(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    report_path = output_dir / "demo_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    report["report_path"] = str(report_path)
    report["snapshot_path"] = str(snapshot_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Piano repair workbench demo")
    parser.add_argument("--output-dir", default="demo_output")
    args = parser.parse_args()
    report = run_demo(args.output_dir)
    summary = {
        "root_id": report["root_id"],
        "operations_applied": report["steps"][0]["operations"],
        "undo_redo_ok": report["steps"][2]["roundtrip_byte_identical"],
        "forced_exit_recovery_ok": report["recovery"]["stable_state_byte_identical"],
        "error_codes_demonstrated": sorted(report["errors"].keys()),
        "report_path": report["report_path"],
        "snapshot_path": report["snapshot_path"],
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
