"""File-based persistence for projects, snapshots and rollback."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .errors import CalibrationError, E_SNAPSHOT_IO
from .models import (
    CalibrationProject,
    DomeSpec,
    ProjectorSpec,
    ControlPointSet,
    BlendingParams,
    ProjectorLUT,
    BlendMask,
    CorrectionQuality,
)


def _project_to_dict(project: CalibrationProject) -> dict:
    return {
        "seed": project.seed,
        "dome": project.dome.to_dict(),
        "projectors": [p.to_dict() for p in project.projectors],
        "control_points": [c.to_dict() for c in project.control_points],
        "blending": project.blending.to_dict(),
        "grid_width": project.grid_width,
        "grid_height": project.grid_height,
        "warp_params": project.warp_params,
        "projector_versions": project.projector_versions,
    }


def project_from_dict(data: dict) -> CalibrationProject:
    return CalibrationProject(
        seed=int(data["seed"]),
        dome=DomeSpec.from_dict(data["dome"]),
        projectors=[ProjectorSpec.from_dict(p) for p in data["projectors"]],
        control_points=[ControlPointSet.from_dict(c) for c in data["control_points"]],
        blending=BlendingParams.from_dict(data["blending"]),
        grid_width=int(data["grid_width"]),
        grid_height=int(data["grid_height"]),
        warp_params=data.get("warp_params", {}),
        projector_versions=dict(data.get("projector_versions", {})),
    )


def save_project(project: CalibrationProject, path: str | Path) -> Path:
    """Save the project definition (no computed artefacts) as JSON."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_project_to_dict(project), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise CalibrationError(
            E_SNAPSHOT_IO, f"failed to write project: {exc}", {"path": str(path)}
        ) from exc
    return path


def load_project(path: str | Path) -> CalibrationProject:
    path = Path(path)
    if not path.exists():
        raise CalibrationError(
            E_SNAPSHOT_IO, "project file not found", {"path": str(path)}
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(
            E_SNAPSHOT_IO, f"failed to read project: {exc}", {"path": str(path)}
        ) from exc
    return project_from_dict(data)


def save_snapshot(project: CalibrationProject, snapshot_dir: str | Path,
                  label: str = "snapshot") -> Path:
    """Persist project + LUTs + masks + quality under a versioned directory."""
    root = Path(snapshot_dir) / label
    try:
        root.mkdir(parents=True, exist_ok=True)
        (root / "project.json").write_text(
            json.dumps(_project_to_dict(project), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        arrays = {}
        for pid, lut in project.luts.items():
            arrays[f"{pid}__lon"] = lut.lon
            arrays[f"{pid}__lat"] = lut.lat
            arrays[f"{pid}__raw_lon"] = lut.raw_lon
            arrays[f"{pid}__raw_lat"] = lut.raw_lat
            arrays[f"{pid}__valid"] = lut.valid
            arrays[f"{pid}__weight"] = project.masks[pid].weight
        np.savez_compressed(root / "arrays.npz", **arrays)
        meta = {
            "label": label,
            "versions": dict(project.projector_versions),
            "luts": {
                pid: {
                    "width": lut.width,
                    "height": lut.height,
                    "spec_hash": lut.spec_hash,
                    "points_hash": lut.points_hash,
                    "blend_hash": lut.blend_hash,
                    "version": lut.version,
                }
                for pid, lut in project.luts.items()
            },
            "quality": {pid: q.to_dict() for pid, q in project.quality.items()},
        }
        (root / "meta.json").write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _append_manifest(snapshot_dir, label, meta["versions"])
    except OSError as exc:
        raise CalibrationError(
            E_SNAPSHOT_IO, f"failed to write snapshot: {exc}",
            {"dir": str(root)},
        ) from exc
    return root


def _append_manifest(snapshot_dir: str | Path, label: str,
                     versions: dict) -> None:
    root = Path(snapshot_dir)
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        manifest = {"snapshots": []}
    manifest["snapshots"] = [s for s in manifest["snapshots"] if s["label"] != label]
    manifest["snapshots"].append({"label": label, "versions": versions})
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def list_snapshots(snapshot_dir: str | Path) -> list[dict]:
    manifest_path = Path(snapshot_dir) / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest.get("snapshots", [])


def load_snapshot(snapshot_dir: str | Path, label: str,
                  expected_versions: dict[str, int] | None = None) -> CalibrationProject:
    """Load a snapshot, optionally rejecting stale projector versions."""
    root = Path(snapshot_dir) / label
    try:
        project = load_project(root / "project.json")
        with np.load(root / "arrays.npz", allow_pickle=False) as npz:
            arrays = {key: npz[key] for key in npz.files}
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
    except (OSError, KeyError) as exc:
        raise CalibrationError(
            E_SNAPSHOT_IO, f"failed to read snapshot '{label}': {exc}",
            {"dir": str(root)},
        ) from exc

    if expected_versions is not None:
        for pid, expected in expected_versions.items():
            stored = meta["versions"].get(pid)
            if stored != expected:
                raise CalibrationError(
                    "E1007_STALE_VERSION_CONFLICT",
                    f"snapshot {label} has stale version for {pid}: "
                    f"stored v{stored} != expected v{expected}",
                    {"projector_id": pid, "stored": stored, "expected": expected},
                )

    for pid, lut_meta in meta["luts"].items():
        valid = arrays[f"{pid}__valid"]
        project.luts[pid] = ProjectorLUT(
            projector_id=pid,
            width=lut_meta["width"],
            height=lut_meta["height"],
            lon=arrays[f"{pid}__lon"],
            lat=arrays[f"{pid}__lat"],
            raw_lon=arrays[f"{pid}__raw_lon"],
            raw_lat=arrays[f"{pid}__raw_lat"],
            valid=valid,
            spec_hash=lut_meta["spec_hash"],
            points_hash=lut_meta["points_hash"],
            blend_hash=lut_meta["blend_hash"],
            version=lut_meta["version"],
        )
        project.masks[pid] = BlendMask(
            projector_id=pid,
            width=lut_meta["width"],
            height=lut_meta["height"],
            weight=arrays[f"{pid}__weight"],
            valid=valid,
            blend_hash=lut_meta["blend_hash"],
            version=lut_meta["version"],
        )
    for pid, q in meta.get("quality", {}).items():
        project.quality[pid] = CorrectionQuality(**q)
    project.projector_versions = dict(meta["versions"])
    return project
