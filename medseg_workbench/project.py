from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .config import TaskConfig, compute_version_id, parse_task, read_stack
from .errors import E_DIGEST, E_STATE, E_VERSION, E_VOLUME, WorkbenchError
from .geometry import Box, Chunk, chunk_grid
from .io_utils import (
    array_digest,
    atomic_write_json,
    atomic_write_npz,
    canonical_digest,
    is_writable_directory,
    load_json,
)
from .scheduler import LogicalClock, plan_rounds, run_plan
from .segmentation import (
    build_dependency_record,
    chunk_array_digest,
    dependency_boxes,
    logical_cost_ms,
    buffer_slot_estimate,
    rasterize_revision_edits,
    region_grow,
    segment_chunk_core,
    segment_full,
)


INTERRUPT_EXIT_CODE = 130


@dataclass
class Revision:
    chunk_id: str
    core_box: Tuple[int, int, int, int, int, int]
    edit_mask: np.ndarray
    values: np.ndarray
    history: List[Dict[str, Any]]

    def digest(self) -> str:
        return canonical_digest(
            {
                "chunk_id": self.chunk_id,
                "core_box": list(self.core_box),
                "edit_digest": array_digest(self.edit_mask),
                "value_digest": array_digest(self.values),
            }
        )


class Project:
    def __init__(
        self,
        cfg: TaskConfig,
        volume: np.ndarray,
        volume_digest: str,
        *,
        version_id: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.volume = volume
        self.volume_digest = volume_digest
        self.version_id = version_id or compute_version_id(cfg, volume_digest)
        self.chunks = chunk_grid(cfg.shape, cfg.chunk_size)
        self.chunks_by_id = {chunk.id: chunk for chunk in self.chunks}
        self.state_dir = cfg.state_dir
        self.chunks_dir = self.state_dir / "chunks"
        self.revisions_dir = self.state_dir / "revisions"
        self.final_revisions_dir = self.state_dir / "final_revisions"
        self.archive_dir = self.state_dir / "archive"
        self.version_path = self.state_dir / "VERSION.json"
        self.interrupt_path = self.state_dir / "INTERRUPTED.json"
        self.last_run_path = self.state_dir / "last_run.json"
        self.last_archive: Optional[Dict[str, Any]] = None

    @classmethod
    def from_task(cls, task_path: Path | str) -> "Project":
        cfg = parse_task(task_path)
        volume, volume_digest = read_stack(cfg, truth=False)
        version_id = compute_version_id(cfg, volume_digest)
        return cls(cfg, volume, volume_digest, version_id=version_id)

    def prepare(self) -> "Project":
        if not is_writable_directory(self.cfg.base_dir):
            raise WorkbenchError(E_STATE, f"working directory is not writable: {self.cfg.base_dir}")
        self.cfg.temp_dir.mkdir(parents=True, exist_ok=True)
        if not is_writable_directory(self.cfg.temp_dir):
            raise WorkbenchError(E_STATE, f"temp directory is not writable: {self.cfg.temp_dir}")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        self.revisions_dir.mkdir(parents=True, exist_ok=True)
        self.final_revisions_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_temps()
        self._sync_version()
        return self

    def _cleanup_stale_temps(self) -> None:
        for directory in (
            self.chunks_dir,
            self.revisions_dir,
            self.final_revisions_dir,
            self.state_dir,
            self.cfg.temp_dir,
            self.cfg.export_dir,
        ):
            if not directory.exists():
                continue
            for path in directory.glob("*.tmp"):
                try:
                    path.unlink()
                except OSError:
                    pass

    def _sync_version(self) -> None:
        payload = {
            "version_id": self.version_id,
            "volume_digest": self.volume_digest,
            "parameters_digest": canonical_digest(self.cfg.normalized_parameters()),
        }
        old_payload: Optional[Dict[str, Any]] = None
        if self.version_path.exists():
            try:
                old_payload = load_json(self.version_path)
            except Exception:
                old_payload = {"version_id": "corrupt-" + canonical_digest({"path": str(self.version_path)})[:12]}
        old_version = str((old_payload or {}).get("version_id", "unknown"))
        if old_payload and old_version != self.version_id:
            archived_to = self._archive_old_version(old_version)
            self.last_archive = {"old_version": old_version, "archived_to": str(archived_to)}
        atomic_write_json(self.version_path, payload)

    def _archive_old_version(self, old_version: str) -> Path:
        safe_version = "".join(ch for ch in old_version if ch.isalnum() or ch in "._-") or "unknown"
        target = self.archive_dir / safe_version
        suffix = 1
        while target.exists():
            target = self.archive_dir / f"{safe_version}-{suffix:03d}"
            suffix += 1
        target.mkdir(parents=True, exist_ok=False)
        if self.chunks_dir.exists():
            shutil.move(str(self.chunks_dir), str(target / "chunks"))
            self.chunks_dir.mkdir(parents=True, exist_ok=True)
        if self.version_path.exists():
            shutil.move(str(self.version_path), str(target / "VERSION.json"))
        return target

    def chunk_path(self, chunk_id: str) -> Path:
        return self.chunks_dir / f"{chunk_id}.npz"

    def revision_path(self, chunk_id: str) -> Path:
        return self.revisions_dir / f"{chunk_id}.npz"

    def final_revision_path(self, chunk_id: str) -> Path:
        return self.final_revisions_dir / f"{chunk_id}.npz"

    def save_chunk(self, chunk: Chunk, mask: np.ndarray, raw: np.ndarray, dependency: Dict[str, Any]) -> str:
        digest = chunk_array_digest(chunk.id, chunk.core, mask, raw, dependency)
        atomic_write_npz(
            self.chunk_path(chunk.id),
            {
                "mask": np.asarray(mask, dtype=np.uint8),
                "raw": np.asarray(raw, dtype=np.uint8),
                "chunk_digest": np.array(digest),
                "version_id": np.array(self.version_id),
                "dependency_json": np.array(json.dumps(dependency, sort_keys=True, ensure_ascii=False)),
            },
        )
        return digest

    def load_chunk(self, chunk: Chunk) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, str]]]:
        path = self.chunk_path(chunk.id)
        if not path.exists():
            return None, {"code": "E_MISSING", "message": "chunk has not completed"}
        try:
            with np.load(path, allow_pickle=False) as data:
                mask = np.asarray(data["mask"], dtype=np.uint8)
                raw = np.asarray(data["raw"], dtype=np.uint8)
                digest = str(data["chunk_digest"].item())
                version_id = str(data["version_id"].item())
                dependency = json.loads(str(data["dependency_json"].item()))
        except Exception as exc:
            return None, {
                "code": E_DIGEST,
                "message": f"chunk payload/digest could not be read: {exc}",
            }

        if mask.shape != chunk.core.shape or raw.shape != chunk.core.shape:
            return None, {
                "code": E_DIGEST,
                "message": "chunk array shape does not match core box",
            }
        expected_digest = chunk_array_digest(chunk.id, chunk.core, mask, raw, dependency)
        if digest != expected_digest:
            return None, {
                "code": E_DIGEST,
                "message": "chunk content digest mismatch",
                "expected": expected_digest,
                "actual": digest,
            }
        if version_id != self.version_id:
            return None, {
                "code": E_VERSION,
                "message": "chunk version identifier mismatch",
                "expected": self.version_id,
                "actual": version_id,
            }
        return {
            "mask": mask,
            "raw": raw,
            "chunk_digest": digest,
            "version_id": version_id,
            "dependency": dependency,
            "path": str(path),
        }, None

    def apply_revision(
        self,
        chunk_id: str,
        edit_mask: np.ndarray,
        values: np.ndarray,
        *,
        source: str = "api",
        note: str = "",
    ) -> Revision:
        if chunk_id not in self.chunks_by_id:
            raise WorkbenchError(E_STATE, f"unknown chunk: {chunk_id}")
        chunk = self.chunks_by_id[chunk_id]
        edit_mask = np.asarray(edit_mask, dtype=bool)
        values = np.asarray(values, dtype=np.uint8)
        if edit_mask.shape != chunk.core.shape or values.shape != chunk.core.shape:
            raise WorkbenchError(
                "E_REVISION",
                "revision arrays must match chunk core shape",
                details={"chunk": chunk_id, "expected_shape": chunk.core.shape},
            )

        old = self.load_revision(chunk_id)
        if old is None:
            merged_mask = edit_mask.copy()
            merged_values = values.copy()
            history: List[Dict[str, Any]] = []
        else:
            merged_mask = old.edit_mask | edit_mask
            merged_values = old.values.copy()
            merged_values[edit_mask] = values[edit_mask]
            history = list(old.history)

        changed_voxels = int(edit_mask.sum())
        history.append(
            {
                "source": source,
                "note": note,
                "touched_voxels": changed_voxels,
                "value_ones": int(values[edit_mask].sum()) if changed_voxels else 0,
            }
        )
        core_box = chunk.core.to_tuple()
        atomic_write_npz(
            self.revision_path(chunk_id),
            {
                "chunk_id": np.array(chunk_id),
                "core_box": np.array(core_box, dtype=np.int64),
                "edit_mask": merged_mask,
                "values": merged_values,
                "history_json": np.array(json.dumps(history, ensure_ascii=False, sort_keys=True)),
            },
        )
        return Revision(chunk_id, core_box, merged_mask, merged_values, history)

    def load_revision(self, chunk_id: str) -> Optional[Revision]:
        path = self.revision_path(chunk_id)
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as data:
                chunk_id_value = str(data["chunk_id"].item())
                core_box = tuple(int(v) for v in data["core_box"].tolist())
                edit_mask = np.asarray(data["edit_mask"], dtype=bool)
                values = np.asarray(data["values"], dtype=np.uint8)
                history = json.loads(str(data["history_json"].item()))
            return Revision(chunk_id_value, core_box, edit_mask, values, history)
        except Exception as exc:
            raise WorkbenchError(E_DIGEST, f"revision file is unreadable: {path}: {exc}") from exc

    def load_revisions(self) -> List[Revision]:
        revisions: List[Revision] = []
        if not self.revisions_dir.exists():
            return revisions
        for path in sorted(self.revisions_dir.glob("*.npz")):
            revision = self.load_revision(path.stem)
            if revision is not None:
                revisions.append(revision)
        return revisions

    def apply_final_revision(
        self,
        chunk_id: str,
        edit_mask: np.ndarray,
        values: np.ndarray,
        *,
        source: str = "api",
        note: str = "",
    ) -> Revision:
        if chunk_id not in self.chunks_by_id:
            raise WorkbenchError(E_STATE, f"unknown chunk: {chunk_id}")
        chunk = self.chunks_by_id[chunk_id]
        edit_mask = np.asarray(edit_mask, dtype=bool)
        values = np.asarray(values, dtype=np.uint8)
        if edit_mask.shape != chunk.core.shape or values.shape != chunk.core.shape:
            raise WorkbenchError(
                "E_REVISION",
                "final revision arrays must match chunk core shape",
                details={"chunk": chunk_id, "expected_shape": chunk.core.shape},
            )
        old = self.load_final_revision(chunk_id)
        if old is None:
            merged_mask = edit_mask.copy()
            merged_values = values.copy()
            history: List[Dict[str, Any]] = []
        else:
            merged_mask = old.edit_mask | edit_mask
            merged_values = old.values.copy()
            merged_values[edit_mask] = values[edit_mask]
            history = list(old.history)
        history.append(
            {
                "layer": "final_mask",
                "source": source,
                "note": note,
                "touched_voxels": int(edit_mask.sum()),
                "value_ones": int(values[edit_mask].sum()) if edit_mask.any() else 0,
            }
        )
        core_box = chunk.core.to_tuple()
        atomic_write_npz(
            self.final_revision_path(chunk_id),
            {
                "chunk_id": np.array(chunk_id),
                "core_box": np.array(core_box, dtype=np.int64),
                "edit_mask": merged_mask,
                "values": merged_values,
                "history_json": np.array(json.dumps(history, ensure_ascii=False, sort_keys=True)),
            },
        )
        return Revision(chunk_id, core_box, merged_mask, merged_values, history)

    def load_final_revision(self, chunk_id: str) -> Optional[Revision]:
        path = self.final_revision_path(chunk_id)
        if not path.exists():
            return None
        try:
            with np.load(path, allow_pickle=False) as data:
                chunk_id_value = str(data["chunk_id"].item())
                core_box = tuple(int(v) for v in data["core_box"].tolist())
                edit_mask = np.asarray(data["edit_mask"], dtype=bool)
                values = np.asarray(data["values"], dtype=np.uint8)
                history = json.loads(str(data["history_json"].item()))
            return Revision(chunk_id_value, core_box, edit_mask, values, history)
        except Exception as exc:
            raise WorkbenchError(E_DIGEST, f"final revision file is unreadable: {path}: {exc}") from exc

    def load_final_revisions(self) -> List[Revision]:
        if not self.final_revisions_dir.exists():
            return []
        result = []
        for path in sorted(self.final_revisions_dir.glob("*.npz")):
            revision = self.load_final_revision(path.stem)
            if revision is not None:
                result.append(revision)
        return result

    def relevant_revision_digests(self, chunk: Chunk) -> List[str]:
        raw_box, _ = dependency_boxes(self.cfg, chunk)
        digests: List[str] = []
        for revision in self.load_revisions():
            if raw_box.intersects(Box(*revision.core_box)):
                digests.append(revision.digest())
        return sorted(digests)

    def revision_state_current(self, chunk: Chunk, record: Optional[Dict[str, Any]]) -> bool:
        if not record:
            return False
        dependency = record.get("dependency") or {}
        applied = dependency.get("applied_revision_digests", [])
        raw_current = list(applied) == self.relevant_revision_digests(chunk)
        expected_final = self.final_acknowledgement_digests().get(chunk.id, [])
        final_current = list(dependency.get("acknowledged_final_revision_digests", [])) == expected_final
        return raw_current and final_current

    def final_acknowledgement_digests(self) -> Dict[str, List[str]]:
        outgoing = self.dependency_graph()["outgoing"]
        impact = self._build_impact_plan(self.load_final_revisions(), "final_mask", outgoing)
        acknowledgements: Dict[str, set] = {}
        for chunk_id, reasons in impact.items():
            digests = {reason["revision_digest"] for reason in reasons if "revision_digest" in reason}
            if digests:
                acknowledgements[chunk_id] = digests
        return {chunk_id: sorted(digests) for chunk_id, digests in acknowledgements.items()}

    def _build_impact_plan(
        self,
        revisions: List[Revision],
        layer: str,
        outgoing: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        revisions_by_id = {revision.chunk_id: revision for revision in revisions}
        impact: Dict[str, List[Dict[str, Any]]] = {}
        self_code = "REV_SELF" if layer == "raw" else "REV_MASK_SELF"
        layer_label = "growth mask" if layer == "raw" else "authoritative final mask"

        for chunk_id in sorted(revisions_by_id):
            revision = revisions_by_id[chunk_id]
            revision_digest = revision.digest()
            impact.setdefault(chunk_id, []).append(
                {
                    "code": self_code,
                    "layer": layer,
                    "severity": "invalid",
                    "revision_digest": revision_digest,
                    "message": f"operator revised {layer_label} in this chunk",
                    "path": [f"revision:{layer}:{chunk_id}", f"{layer}:{chunk_id}", f"final:{chunk_id}"],
                }
            )
            for edge in outgoing.get(f"raw:{chunk_id}", []):
                dependent_id = edge["to"].split(":", 1)[1]
                if dependent_id == chunk_id or edge["type"] != "neighbor_padding":
                    continue
                detail = edge["detail"]
                dependency_box = Box(*detail["range"])
                revision_core = Box(*revision.core_box)
                overlap = dependency_box.intersection(revision_core)
                local_slices = (
                    slice(overlap.z0 - revision_core.z0, overlap.z1 - revision_core.z0),
                    slice(overlap.y0 - revision_core.y0, overlap.y1 - revision_core.y0),
                    slice(overlap.x0 - revision_core.x0, overlap.x1 - revision_core.x0),
                )
                affected_revision_voxels = int(revision.edit_mask[local_slices].sum())
                if affected_revision_voxels == 0:
                    continue
                impact.setdefault(dependent_id, []).append(
                    {
                        "code": "REV_BOUNDARY_DEPENDENCY",
                        "layer": layer,
                        "severity": "invalid",
                        "revision_digest": revision_digest,
                        "message": (
                            f"{dependent_id} must recheck boundary stitching after "
                            f"{layer_label} revision in {chunk_id}"
                        ),
                        "path": [f"{layer}:{chunk_id}", edge["to"]],
                        "dependency": {
                            **detail,
                            "affected_revision_voxels": affected_revision_voxels,
                        },
                    }
                )
        return impact

    def dependency_graph(self) -> Dict[str, Any]:
        nodes: List[Dict[str, str]] = []
        edges: List[Dict[str, Any]] = []
        outgoing: Dict[str, List[Dict[str, Any]]] = {}
        for chunk in self.chunks:
            dep = build_dependency_record(
                self.cfg, chunk, self.chunks, self.volume, self.version_id
            )
            raw_node = f"raw:{chunk.id}"
            final_node = f"final:{chunk.id}"
            nodes.extend([
                {"id": raw_node, "kind": "raw_chunk", "chunk": chunk.id},
                {"id": final_node, "kind": "final_chunk", "chunk": chunk.id},
            ])
            self_edge = {"from": raw_node, "to": final_node, "type": "self"}
            edges.append(self_edge)
            outgoing.setdefault(raw_node, []).append(self_edge)
            for neighbor in dep["neighbor_padding"]:
                provider = neighbor["chunk"]
                edge = {
                    "from": f"raw:{provider}",
                    "to": final_node,
                    "type": "neighbor_padding",
                    "detail": neighbor,
                }
                edges.append(edge)
                outgoing.setdefault(f"raw:{provider}", []).append(edge)
        return {"nodes": nodes, "edges": edges, "outgoing": outgoing}

    def invalidation_plan(self) -> Dict[str, Any]:
        graph = self.dependency_graph()
        outgoing = graph["outgoing"]
        raw_revisions = self.load_revisions()
        final_revisions = self.load_final_revisions()
        raw_impact = self._build_impact_plan(raw_revisions, "raw", outgoing)
        final_impact = self._build_impact_plan(final_revisions, "final_mask", outgoing)
        invalid: Dict[str, List[Dict[str, Any]]] = {}
        for impact in (raw_impact, final_impact):
            for chunk_id, reasons in impact.items():
                invalid.setdefault(chunk_id, []).extend(reasons)
        revised_ids = {revision.chunk_id for revision in raw_revisions + final_revisions}
        paths = {
            chunk_id: [reason["path"] for reason in reasons if "path" in reason]
            for chunk_id, reasons in invalid.items()
        }

        reason_counts: Dict[str, int] = {}
        for reasons in invalid.values():
            for reason in reasons:
                reason_counts[reason["code"]] = reason_counts.get(reason["code"], 0) + 1

        stale: Dict[str, List[Dict[str, Any]]] = {}
        for chunk_id, reasons in invalid.items():
            chunk = self.chunks_by_id[chunk_id]
            record, issue = self.load_chunk(chunk)
            if issue is not None or not self.revision_state_current(chunk, record):
                stale_reasons = list(reasons)
                if issue is not None:
                    stale_reasons.append(
                        {
                            "code": issue.get("code", E_STATE),
                            "severity": "invalid",
                            "message": issue.get("message", "persisted chunk is not reusable"),
                        }
                    )
                stale[chunk_id] = stale_reasons
        return {
            "invalid_chunk_ids": sorted(invalid),
            "invalid_count": len(invalid),
            "stale_chunk_ids": sorted(stale),
            "stale_count": len(stale),
            "stale_reasons": stale,
            "revised_chunk_ids": sorted(revised_ids),
            "reasons": invalid,
            "raw_impacted_chunk_ids": sorted(raw_impact),
            "final_impacted_chunk_ids": sorted(final_impact),
            "paths": paths,
            "reason_counts": reason_counts,
            "graph_summary": {"node_count": len(graph["nodes"]), "edge_count": len(graph["edges"])},
        }

    def chunk_states(self) -> List[Dict[str, Any]]:
        invalidation = self.invalidation_plan()
        stale_set = set(invalidation["stale_chunk_ids"])
        revised_set = set(invalidation["revised_chunk_ids"])
        states: List[Dict[str, Any]] = []
        for chunk in self.chunks:
            record, issue = self.load_chunk(chunk)
            if chunk.id in stale_set:
                status = "invalid"
            elif record is not None:
                status = "complete"
            else:
                status = "pending"
            states.append(
                {
                    "chunk_id": chunk.id,
                    "index": list(chunk.index),
                    "core_box": chunk.core.to_tuple(),
                    "status": status,
                    "revised": chunk.id in revised_set,
                    "digest_ok": record is not None,
                    "issue": issue,
                    "invalidation_reasons": invalidation["stale_reasons"].get(
                        chunk.id, invalidation["reasons"].get(chunk.id, [])
                    ),
                    "dependency_impacted": chunk.id in invalidation["invalid_chunk_ids"],
                    "dependency": (record or {}).get("dependency"),
                }
            )
        return states

    def run(
        self,
        *,
        stop_event: Optional[threading.Event] = None,
        clock: Optional[LogicalClock] = None,
        chunk_real_delay: float = 0.0,
    ) -> Dict[str, Any]:
        self.prepare()
        clock = clock or LogicalClock()
        invalidation = self.invalidation_plan()
        invalid_set = set(invalidation["stale_chunk_ids"])

        pending: List[Chunk] = []
        reused: List[str] = []
        digest_recoveries: List[Dict[str, Any]] = []
        for chunk in self.chunks:
            if chunk.id in invalid_set:
                pending.append(chunk)
                continue
            record, issue = self.load_chunk(chunk)
            if record is not None:
                reused.append(chunk.id)
            else:
                pending.append(chunk)
                if issue and issue.get("code") in {E_DIGEST, E_VERSION}:
                    digest_recoveries.append({"chunk": chunk.id, **issue})

        plan = plan_rounds(
            pending,
            parallelism=self.cfg.parallelism,
            memory_budget_slots=self.cfg.pixel_memory_budget,
            time_budget_ms=self.cfg.logic_time_budget_ms,
            memory_cost=lambda c: buffer_slot_estimate(self.cfg, c),
            logical_cost=lambda c: logical_cost_ms(self.cfg, c),
        )
        revisions = self.load_revisions()
        final_acknowledgements = self.final_acknowledgement_digests()

        def process_chunk(chunk: Chunk) -> None:
            raw_box, _ = dependency_boxes(self.cfg, chunk)
            raw_padded = region_grow(
                self.volume,
                self.cfg.threshold,
                self.cfg.seed_stride,
                self.cfg.growth_radius,
                target_box=raw_box,
            )
            edit_mask, edit_values = rasterize_revision_edits(
                self.cfg, raw_box, revisions
            )
            if edit_mask.any():
                raw_padded = raw_padded.copy()
                raw_padded[edit_mask] = edit_values[edit_mask].astype(bool)

            computed = segment_chunk_core(
                self.volume,
                self.cfg,
                chunk,
                raw_edits=(edit_mask, edit_values),
            )
            # segment_chunk_core re-runs RG internally for a pure API; use the
            # already rasterized result by overriding its raw computation path.
            # The small duplicate calculation keeps the kernel function pure.
            mask = np.asarray(computed["mask"], dtype=np.uint8)
            raw = np.asarray(computed["raw"], dtype=np.uint8)
            dependency = build_dependency_record(
                self.cfg, chunk, self.chunks, self.volume, self.version_id
            )
            dependency["applied_revision_digests"] = self.relevant_revision_digests(chunk)
            dependency["acknowledged_final_revision_digests"] = final_acknowledgements.get(chunk.id, [])
            final_revision = self.load_final_revision(chunk.id)
            if final_revision is not None:
                mask = mask.copy()
                mask[final_revision.edit_mask] = final_revision.values[final_revision.edit_mask]
            self.save_chunk(chunk, mask, raw, dependency)

        run_result = run_plan(
            plan,
            self.chunks_by_id,
            parallelism=self.cfg.parallelism,
            process_chunk=process_chunk,
            clock=clock,
            stop_event=stop_event,
            chunk_real_delay=chunk_real_delay,
        )
        if run_result.failed:
            raise WorkbenchError(
                E_STATE,
                "one or more chunks failed",
                details={"failed": run_result.failed},
            )

        report: Dict[str, Any] = {
            "ok": True,
            "interrupted": False,
            "version_id": self.version_id,
            "plan": plan.to_dict(),
            "completed_this_run": run_result.completed,
            "reused_chunks": sorted(reused),
            "pending_count": len(pending),
            "reused_count": len(reused),
            "digest_recoveries": digest_recoveries,
            "invalidation": invalidation,
            "clock_logical_ms": clock.now(),
            "archive": self.last_archive,
        }

        if run_result.interrupted:
            interrupt_payload = {
                "code": "E_INTERRUPTED",
                "exit_code": INTERRUPT_EXIT_CODE,
                "version_id": self.version_id,
                "completed_this_run": run_result.completed,
                "pending_admission_sequence": plan.admission_sequence,
                "plan": plan.to_dict(),
            }
            atomic_write_json(self.interrupt_path, interrupt_payload)
            report["interrupted"] = True
            report["exit_code"] = INTERRUPT_EXIT_CODE
            return report

        missing = [chunk.id for chunk in self.chunks if self.load_chunk(chunk)[0] is None]
        if missing:
            raise WorkbenchError(
                E_STATE,
                "scheduler finished but chunks are missing",
                details={"missing": missing},
            )
        self.interrupt_path.unlink(missing_ok=True)
        atomic_write_json(self.last_run_path, report)
        report["assembled_shape"] = list(self.cfg.shape)
        return report

    def assemble_mask(self, layer: str = "mask") -> np.ndarray:
        if layer not in {"mask", "raw"}:
            raise ValueError("layer must be mask or raw")
        output = np.zeros(self.cfg.shape, dtype=np.uint8)
        missing: List[str] = []
        for chunk in self.chunks:
            record, issue = self.load_chunk(chunk)
            if record is None:
                missing.append(chunk.id)
                continue
            box = chunk.core
            output[box.z0 : box.z1, box.y0 : box.y1, box.x0 : box.x1] = record[layer]
        if missing:
            raise WorkbenchError(E_STATE, "cannot assemble incomplete chunks", details={"missing": missing})
        return output

    def seam_report(self) -> Dict[str, Any]:
        """Digest every positive-direction chunk interface for diagnostic checks."""
        seams: List[Dict[str, Any]] = []
        for chunk in self.chunks:
            record, issue = self.load_chunk(chunk)
            if record is None:
                continue
            index = chunk.index
            for axis, delta_index in enumerate(((1, 0, 0), (0, 1, 0), (0, 0, 1))):
                neighbor_index = (
                    index[0] + delta_index[0],
                    index[1] + delta_index[1],
                    index[2] + delta_index[2],
                )
                neighbor = self.chunks_by_id.get(
                    f"{neighbor_index[0]:03d}_{neighbor_index[1]:03d}_{neighbor_index[2]:03d}"
                )
                if neighbor is None:
                    continue
                neighbor_record, neighbor_issue = self.load_chunk(neighbor)
                if neighbor_record is None:
                    continue
                if axis == 0:
                    face_a = record["mask"][-1, :, :]
                    face_b = neighbor_record["mask"][0, :, :]
                elif axis == 1:
                    face_a = record["mask"][:, -1, :]
                    face_b = neighbor_record["mask"][:, 0, :]
                else:
                    face_a = record["mask"][:, :, -1]
                    face_b = neighbor_record["mask"][:, :, 0]
                seams.append(
                    {
                        "a": chunk.id,
                        "b": neighbor.id,
                        "axis": ("z", "y", "x")[axis],
                        "interface_digest": canonical_digest(
                            {"a": array_digest(face_a), "b": array_digest(face_b)}
                        )[:16],
                    }
                )
        return {"seam_count": len(seams), "seams": seams}

    def global_raw_with_revisions(self) -> np.ndarray:
        raw = region_grow(
            self.volume,
            self.cfg.threshold,
            self.cfg.seed_stride,
            self.cfg.growth_radius,
            target_box=Box.from_shape(self.cfg.shape),
        )
        revisions = self.load_revisions()
        for revision in revisions:
            core_box = Box(*revision.core_box)
            slices = (
                slice(core_box.z0, core_box.z1),
                slice(core_box.y0, core_box.y1),
                slice(core_box.x0, core_box.x1),
            )
            region = raw[slices]
            region[revision.edit_mask] = revision.values[revision.edit_mask].astype(bool)
        return raw

    def full_reference(self) -> Tuple[np.ndarray, np.ndarray]:
        raw_override = self.global_raw_with_revisions()
        raw, mask = segment_full(self.volume, self.cfg, raw_override_global=raw_override)
        for revision in self.load_final_revisions():
            core_box = Box(*revision.core_box)
            slices = (
                slice(core_box.z0, core_box.z1),
                slice(core_box.y0, core_box.y1),
                slice(core_box.x0, core_box.x1),
            )
            region = mask[slices]
            region[revision.edit_mask] = revision.values[revision.edit_mask].astype(np.uint8)
        return raw, mask

    def demo_revision(self, chunk_id: Optional[str] = None, *, layer: str = "raw") -> Revision:
        if chunk_id is None:
            index = (
                self.cfg.grid_count[0] // 2,
                self.cfg.grid_count[1] // 2,
                self.cfg.grid_count[2] // 2,
            )
            chunk_id = f"{index[0]:03d}_{index[1]:03d}_{index[2]:03d}"
        chunk = self.chunks_by_id[chunk_id]
        edit_mask = np.zeros(chunk.core.shape, dtype=bool)
        values = np.zeros(chunk.core.shape, dtype=np.uint8)
        z0 = 0
        z1 = min(4, chunk.core.shape[0])
        y0 = chunk.core.shape[1] // 2 - 2
        y1 = y0 + 4
        x0 = chunk.core.shape[2] // 2 - 2
        x1 = x0 + 4
        edit_mask[z0:z1, y0:y1, x0:x1] = True
        values[:] = 0
        apply_edit = self.apply_revision if layer == "raw" else self.apply_final_revision
        return apply_edit(
            chunk_id,
            edit_mask,
            values,
            source=f"demo-{layer}",
            note="deterministic eraser touching a chunk boundary",
        )

    def demo_final_revision(self, chunk_id: Optional[str] = None) -> Revision:
        return self.demo_revision(chunk_id, layer="final_mask")


def open_prepared_project(task_path: Path | str) -> Project:
    return Project.from_task(task_path).prepare()
