"""Versioned snapshots with temp-file + atomic replace and staleness checks."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from .errors import E_SNAPSHOT_STALE, FormatInvalid, SnapshotStale
from .fingerprint import canonical_json, loss_log_hash, structure_fingerprint
from .editor import edit_log_summary
from .models import UnifiedProject
from . import parsers

SNAPSHOT_SUFFIX = ".snapshot.json"
PROGRESS_SUFFIX = ".progress.json"


@dataclass
class Snapshot:
    global_version: int
    project_fingerprint: str
    loss_log_hash: str
    edit_log_summary: Dict[str, Any]
    project_file: str
    project_version: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "global_version": self.global_version,
            "project_fingerprint": self.project_fingerprint,
            "loss_log_hash": self.loss_log_hash,
            "edit_log_summary": self.edit_log_summary,
            "project_file": self.project_file,
            "project_version": self.project_version,
        }


def snapshot_path_for(work_dir: Path, project_id: str) -> Path:
    return Path(work_dir) / f"{project_id}{SNAPSHOT_SUFFIX}"


def progress_path_for(work_dir: Path, project_id: str) -> Path:
    return Path(work_dir) / f"{project_id}{PROGRESS_SUFFIX}"


def write_snapshot(project: UnifiedProject, project_file: Path,
                   work_dir: Path, global_version: int) -> Path:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    snapshot = Snapshot(
        global_version=int(global_version),
        project_fingerprint=structure_fingerprint(project),
        loss_log_hash=loss_log_hash(project.loss_log),
        edit_log_summary=edit_log_summary(project),
        project_file=str(project_file),
        project_version=project.source_version,
    )
    target = snapshot_path_for(work_dir, project.project_id)
    tmp = Path(str(target) + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snapshot.to_dict(), fh, ensure_ascii=False,
                  indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)
    return target


def load_snapshot(project: UnifiedProject, work_dir: Path) -> Snapshot:
    target = snapshot_path_for(work_dir, project.project_id)
    if not target.exists():
        raise FormatInvalid(f"快照不存在: {target}",
                            context={"path": str(target)})
    with open(target, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    required = {"global_version", "project_fingerprint", "loss_log_hash",
                "edit_log_summary", "project_file", "project_version"}
    missing = required - set(data)
    if missing:
        raise FormatInvalid("快照结构不完整",
                            context={"missing": sorted(missing)})
    current_fp = structure_fingerprint(project)
    if data["project_fingerprint"] != current_fp:
        raise SnapshotStale(
            "快照指纹与当前工程内容不符，拒绝静默读取旧快照",
            code=E_SNAPSHOT_STALE,
            context={"snapshot_fingerprint": data["project_fingerprint"],
                     "current_fingerprint": current_fp})
    current_loss_hash = loss_log_hash(project.loss_log)
    if data["loss_log_hash"] != current_loss_hash:
        raise SnapshotStale(
            "降级损失清单哈希与当前工程不符，拒绝恢复",
            code=E_SNAPSHOT_STALE,
            context={"snapshot_loss_hash": data["loss_log_hash"],
                     "current_loss_hash": current_loss_hash})
    return Snapshot(
        global_version=int(data["global_version"]),
        project_fingerprint=current_fp,
        loss_log_hash=current_loss_hash,
        edit_log_summary=dict(data["edit_log_summary"]),
        project_file=str(data["project_file"]),
        project_version=int(data["project_version"]),
    )


def write_progress(work_dir: Path, project_id: str, last_op_seq: int,
                   snapshot_version: int) -> Path:
    target = progress_path_for(work_dir, project_id)
    payload = {
        "project_id": project_id,
        "last_completed_op_seq": int(last_op_seq),
        "snapshot_global_version": int(snapshot_version),
    }
    tmp = Path(str(target) + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2,
                  sort_keys=True)
        fh.write("\n")
    os.replace(tmp, target)
    return target


def read_progress(work_dir: Path, project_id: str) -> Dict[str, Any]:
    target = progress_path_for(work_dir, project_id)
    if not target.exists():
        return {"project_id": project_id,
                "last_completed_op_seq": -1,
                "snapshot_global_version": 0}
    with open(target, "r", encoding="utf-8") as fh:
        return json.load(fh)
