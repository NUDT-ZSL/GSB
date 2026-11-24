from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

from .config import read_stack
from .io_utils import array_digest, atomic_write_json, atomic_write_npz, canonical_digest, load_json
from .project import Project


def load_truth_stack(project: Project) -> np.ndarray:
    truth, _ = read_stack(project.cfg, truth=True)
    return truth


def overlap_statistics(prediction: np.ndarray, truth: np.ndarray) -> Dict[str, Any]:
    pred = np.asarray(prediction).astype(bool)
    target = np.asarray(truth)
    if target.dtype.kind in {"i", "u"}:
        target_bool = target != 0
    else:
        target_bool = target.astype(bool)

    intersection = int(np.logical_and(pred, target_bool).sum())
    pred_sum = int(pred.sum())
    truth_sum = int(target_bool.sum())
    union = int(np.logical_or(pred, target_bool).sum())
    dice = (2.0 * intersection) / (pred_sum + truth_sum) if pred_sum + truth_sum else 1.0
    iou = intersection / union if union else 1.0
    precision = intersection / pred_sum if pred_sum else 1.0
    recall = intersection / truth_sum if truth_sum else 1.0

    slice_dice = []
    for z in range(pred.shape[0]):
        p = pred[z]
        t = target_bool[z]
        inter = int(np.logical_and(p, t).sum())
        denom = int(p.sum() + t.sum())
        slice_dice.append((2.0 * inter / denom) if denom else 1.0)

    return {
        "dice": dice,
        "iou": iou,
        "precision": precision,
        "recall": recall,
        "prediction_voxels": pred_sum,
        "truth_voxels": truth_sum,
        "intersection_voxels": intersection,
        "union_voxels": union,
        "slice_dice": slice_dice,
        "mean_slice_dice": float(np.mean(slice_dice)) if slice_dice else 1.0,
    }


def build_evaluation_report(project: Project) -> Dict[str, Any]:
    mask = project.assemble_mask("mask")
    raw = project.assemble_mask("raw")
    truth = load_truth_stack(project)
    stats = overlap_statistics(mask, truth)
    invalidation = project.invalidation_plan()
    states = project.chunk_states()
    status_counts: Dict[str, int] = {}
    revised_count = 0
    digest_ok_count = 0
    for state in states:
        status_counts[state["status"]] = status_counts.get(state["status"], 0) + 1
        revised_count += int(state["revised"])
        digest_ok_count += int(state["digest_ok"])

    return {
        "ok": True,
        "version_id": project.version_id,
        "truth_usage": "evaluation_only; truth is never read by segmentation or invalidation",
        "mask_digest": array_digest(mask),
        "raw_digest": array_digest(raw),
        "overlap": stats,
        "invalidation": {
            "invalid_count": invalidation["stale_count"],
            "invalid_chunk_ids": invalidation["stale_chunk_ids"],
            "impacted_count": invalidation["invalid_count"],
            "impacted_chunk_ids": invalidation["invalid_chunk_ids"],
            "revised_chunk_ids": invalidation["revised_chunk_ids"],
            "reason_counts": invalidation["reason_counts"],
            "stale_reason_counts": {
                code: sum(1 for reasons in invalidation["stale_reasons"].values() for reason in reasons if reason.get("code") == code)
                for code in sorted({reason.get("code") for reasons in invalidation["stale_reasons"].values() for reason in reasons})
            },
        },
        "chunks": {
            "total": len(project.chunks),
            "status_counts": status_counts,
            "revised_count": revised_count,
            "digest_ok_count": digest_ok_count,
        },
        "spacing_zyx": list(project.cfg.spacing),
    }


def export_mask(project: Project, path: Optional[Path | str] = None, *, force: bool = False) -> Dict[str, Any]:
    mask = project.assemble_mask("mask")
    raw = project.assemble_mask("raw")
    path = Path(path) if path is not None else project.cfg.export_dir / "mask.npz"
    revision_digests = [revision.digest() for revision in project.load_revisions()]
    revision_digests.extend(revision.digest() for revision in project.load_final_revisions())
    export_id = canonical_digest(
        {
            "version_id": project.version_id,
            "mask_digest": array_digest(mask),
            "raw_digest": array_digest(raw),
            "revisions": revision_digests,
        }
    )[:20]

    old_export_id = None
    old_version_id = None
    if path.exists():
        try:
            with np.load(path, allow_pickle=False) as data:
                old_export_id = str(data["export_id"].item())
                old_version_id = str(data["version_id"].item())
        except Exception:
            old_export_id = None

    if not force and old_export_id == export_id:
        return {
            "path": str(path),
            "action": "unchanged",
            "version_id": project.version_id,
            "export_id": export_id,
            "existing_version_id": old_version_id,
        }

    atomic_write_npz(
        path,
        {
            "mask": mask,
            "raw": raw,
            "version_id": np.array(project.version_id),
            "export_id": np.array(export_id),
            "spacing_zyx": np.array(project.cfg.spacing, dtype=np.float64),
        },
    )
    return {
        "path": str(path),
        "action": "replaced",
        "version_id": project.version_id,
        "export_id": export_id,
        "old_export_id": old_export_id,
        "old_version_id": old_version_id,
    }


def export_report(report: Dict[str, Any], path: Optional[Path | str] = None, *, force: bool = False) -> Dict[str, Any]:
    if path is None:
        raise ValueError("report export path is required")
    path = Path(path)
    body = dict(report)
    body.pop("export_id", None)
    export_id = canonical_digest(body)[:20]
    old_export_id = None
    if path.exists():
        try:
            old = load_json(path)
            old_export_id = old.get("export_id")
        except Exception:
            old_export_id = None
    if not force and old_export_id == export_id:
        return {"path": str(path), "action": "unchanged", "export_id": export_id}
    payload = dict(body)
    payload["export_id"] = export_id
    atomic_write_json(path, payload)
    return {"path": str(path), "action": "replaced", "export_id": export_id}
