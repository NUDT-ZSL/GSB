"""Versioned snapshots with atomic directory replacement and recovery."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_SNAPSHOT_CORRUPT, PipelineError
from .models import InstanceRecord
from .fusion import FusionResult

CURRENT_LINK = "current.json"
MANIFEST_NAME = "manifest.json"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


@dataclass
class SnapshotStore:
    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def current_pointer(self) -> Path:
        return self.root / CURRENT_LINK

    def snapshot_dir(self, version: int) -> Path:
        return self.root / f"snapshot-{version:06d}"

    def current_version(self) -> int | None:
        if not self.current_pointer.exists():
            return None
        try:
            pointer = json.loads(self.current_pointer.read_text(encoding="utf-8"))
            return int(pointer["version"])
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PipelineError(E_SNAPSHOT_CORRUPT, f"current pointer unreadable: {exc}")

    def load_manifest(self, version: int | None = None) -> dict[str, Any]:
        version = self.current_version() if version is None else version
        if version is None:
            raise PipelineError(E_SNAPSHOT_CORRUPT, "no snapshot exists")
        manifest_path = self.snapshot_dir(version) / MANIFEST_NAME
        if not manifest_path.exists():
            raise PipelineError(E_SNAPSHOT_CORRUPT, f"manifest missing for version {version}")
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def commit(
        self,
        version: int,
        manifest: dict[str, Any],
        instances: dict[str, InstanceRecord],
        fusion_results: dict[str, FusionResult],
    ) -> Path:
        target = self.snapshot_dir(version)
        if target.exists():
            shutil.rmtree(target)
        parent = self.root
        temp_dir = Path(tempfile.mkdtemp(prefix=".snap-tmp-", dir=parent))
        try:
            instances_dir = temp_dir / "instances"
            instances_dir.mkdir()
            serialized_instances = {}
            for instance_id, record in sorted(instances.items()):
                file_name = f"{_safe_name(instance_id)}.json"
                atomic_write_json(instances_dir / file_name, record.to_json_dict())
                serialized_instances[instance_id] = file_name

            fusion_dir = temp_dir / "fusion"
            fusion_dir.mkdir()
            serialized_fusion = {}
            for scene_id, result in sorted(fusion_results.items()):
                file_name = f"{_safe_name(scene_id)}.json"
                atomic_write_json(fusion_dir / file_name, _serialize_fusion(result))
                serialized_fusion[scene_id] = file_name

            full_manifest = dict(manifest)
            full_manifest["instance_files"] = serialized_instances
            full_manifest["fusion_files"] = serialized_fusion
            atomic_write_json(temp_dir / MANIFEST_NAME, full_manifest)

            os.replace(temp_dir, target)
            atomic_write_json(
                self.current_pointer,
                {"version": version, "snapshot": target.name, "manifest": full_manifest["global_version"]},
            )
            self._prune(version)
            return target
        except BaseException:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise

    def recover(self) -> tuple[dict[str, Any], dict[str, InstanceRecord], dict[str, FusionResult]] | None:
        version = self.current_version()
        if version is None:
            return None
        snapshot_dir = self.snapshot_dir(version)
        if not (snapshot_dir / MANIFEST_NAME).exists():
            raise PipelineError(E_SNAPSHOT_CORRUPT, f"current snapshot {version} is incomplete")
        manifest = self.load_manifest(version)
        instances: dict[str, InstanceRecord] = {}
        for instance_id, file_name in manifest["instance_files"].items():
            instances[instance_id] = InstanceRecord.from_json_dict(
                json.loads((snapshot_dir / "instances" / file_name).read_text(encoding="utf-8"))
            )
        fusion_results: dict[str, FusionResult] = {}
        for scene_id, file_name in manifest["fusion_files"].items():
            fusion_results[scene_id] = _deserialize_fusion(
                json.loads((snapshot_dir / "fusion" / file_name).read_text(encoding="utf-8"))
            )
        return manifest, instances, fusion_results

    def _prune(self, active_version: int) -> None:
        for child in self.root.iterdir():
            if child.name.startswith("snapshot-") and child.is_dir():
                number = int(child.name.split("-", 1)[1])
                if number != active_version:
                    shutil.rmtree(child, ignore_errors=True)
            if child.name.startswith(".snap-tmp-"):
                shutil.rmtree(child, ignore_errors=True)


def _safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else f"_{ord(ch):04x}" for ch in name)


def _serialize_fusion(result: FusionResult) -> dict[str, Any]:
    return {
        "scene_id": result.scene_id,
        "width": result.width,
        "height": result.height,
        "fused_instances": [item.to_dict() for item in result.fused_instances],
        "conflicts": [item.to_dict() for item in result.conflicts],
        "boundary_probes": [item.to_dict() for item in result.boundary_probes],
        "dropped_instances": list(result.dropped_instances),
        "label_map": result.label_map.tolist() if result.label_map is not None else None,
    }


def _deserialize_fusion(payload: dict[str, Any]) -> FusionResult:
    from .fusion import BoundaryProbe, ConflictRecord, FusedInstance

    result = FusionResult(scene_id=payload["scene_id"], width=payload["width"], height=payload["height"])
    result.dropped_instances = list(payload.get("dropped_instances", []))
    for item in payload["fused_instances"]:
        result.fused_instances.append(
            FusedInstance(
                fused_id=item["fused_id"],
                scene_id=item["scene_id"],
                semantic_class=item["semantic_class"],
                width=payload["width"],
                height=payload["height"],
                runs=tuple((start, length) for start, length in item["runs"]),
                pixel_count=item["pixel_count"],
                confidence=item["confidence"],
                is_crowd=item["is_crowd"],
                contributors=list(item["contributors"]),
                source_versions=list(item["source_versions"]),
                match_iou=item["match_iou"],
            )
        )
    result.conflicts = [ConflictRecord(**item) for item in payload.get("conflicts", [])]
    result.boundary_probes = [
        BoundaryProbe(
            instance_id=item["instance_id"],
            scene_id=item["scene_id"],
            kind=item["kind"],
            reference_value=float(item["reference_value"]),
            actual_value=float(item["actual_value"]),
            difference=float(item["difference"]),
            reference_decision=item["reference_decision"],
            actual_decision=item["actual_decision"],
            sensitive=item["sensitive"],
            sort_key=item["sort_key"],
        )
        for item in payload.get("boundary_probes", [])
    ]
    if payload.get("label_map") is not None:
        import numpy as np

        result.label_map = np.asarray(payload["label_map"], dtype=np.uint16)
    return result
