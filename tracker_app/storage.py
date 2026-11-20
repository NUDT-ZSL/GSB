from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .errors import TrackingError
from .jsonio import (
    atomic_write_json,
    atomic_write_jsonl,
    canonical_json,
    canonical_line,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_json,
)


CHECKPOINT_SCHEMA = "mt-checkpoint-v1"


def revision_dirname(revision: int) -> str:
    return f"r{revision:06d}"


class SequenceStore:
    def __init__(self, runtime_dir: str | Path, seq_id: str) -> None:
        self.root = Path(runtime_dir) / seq_id
        self.seq_id = seq_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.root / "meta.json"
        self.revisions_root = self.root / "revisions"
        self.checkpoints_root = self.root / "checkpoints"
        self.revisions_root.mkdir(parents=True, exist_ok=True)
        self.checkpoints_root.mkdir(parents=True, exist_ok=True)

    def _revision_dir(self, revision: int) -> Path:
        return self.checkpoints_root / revision_dirname(revision)

    def _input_dir(self, revision: int) -> Path:
        return self.revisions_root / revision_dirname(revision)

    def load_meta(self) -> dict[str, Any]:
        if not self.meta_path.exists():
            return {
                "schema": "mt-store-meta-v1",
                "seq_id": self.seq_id,
                "current_revision": None,
                "revisions": {},
            }
        return read_json(self.meta_path)

    def save_meta(self, meta: dict[str, Any]) -> None:
        atomic_write_json(self.meta_path, meta, pretty=True)

    def register_input(
        self,
        revision: int,
        detection_rows: Iterable[dict[str, Any]],
        truth_rows: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        detection_list = list(detection_rows)
        truth_list = list(truth_rows)
        det_bytes = "".join(canonical_line(row) for row in detection_list).encode(
            "utf-8"
        )
        truth_bytes = "".join(canonical_line(row) for row in truth_list).encode(
            "utf-8"
        )
        det_checksum = sha256_bytes(det_bytes)
        truth_checksum = sha256_bytes(truth_bytes)
        revision_dir = self._input_dir(revision)
        revision_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_jsonl(revision_dir / "detections.jsonl", detection_list)
        atomic_write_jsonl(revision_dir / "truth.jsonl", truth_list)
        record = {
            "revision": revision,
            "detections_sha256": det_checksum,
            "truth_sha256": truth_checksum,
            "detection_count": len(detection_list),
            "truth_count": len(truth_list),
        }
        atomic_write_json(revision_dir / "input_meta.json", record, pretty=True)
        meta = self.load_meta()
        previous = meta.get("revisions", {})
        previous[str(revision)] = record
        meta["revisions"] = {key: previous[key] for key in sorted(previous, key=int)}
        meta["current_revision"] = revision
        self.save_meta(meta)
        return record

    def input_meta(self, revision: int) -> dict[str, Any] | None:
        path = self._input_dir(revision) / "input_meta.json"
        return read_json(path) if path.exists() else None

    def load_input_detections(self, revision: int) -> list[dict[str, Any]]:
        path = self._input_dir(revision) / "detections.jsonl"
        if not path.exists():
            raise TrackingError(
                "E_REVISION_INVALID",
                "stored detection snapshot is missing",
                seq_id=self.seq_id,
                category="missing_revision_snapshot",
                context={"revision": revision},
            )
        return read_jsonl(path)

    def load_input_truth(self, revision: int) -> list[dict[str, Any]]:
        path = self._input_dir(revision) / "truth.jsonl"
        if not path.exists():
            raise TrackingError(
                "E_REVISION_INVALID",
                "stored truth snapshot is missing",
                seq_id=self.seq_id,
                category="missing_revision_snapshot",
                context={"revision": revision},
            )
        return read_jsonl(path)

    def checkpoint_paths(self, revision: int | None = None) -> list[Path]:
        roots: list[Path]
        if revision is None:
            roots = sorted(self.checkpoints_root.glob("r*"))
        else:
            roots = [self._revision_dir(revision)]
        paths: list[Path] = []
        for root in roots:
            if root.exists():
                paths.extend(root.glob("*.json"))
        return sorted(paths, key=lambda item: (int(item.stem), str(item.parent)))

    def _next_global_version(self) -> int:
        maximum = 0
        for path in self.checkpoint_paths():
            try:
                maximum = max(maximum, int(path.stem))
            except ValueError:
                continue
        return maximum + 1

    @staticmethod
    def _checkpoint_payload(checkpoint: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in checkpoint.items() if key != "checksum"}

    def read_checkpoint(
        self,
        path: str | Path,
        *,
        verify: bool = True,
    ) -> dict[str, Any]:
        checkpoint_path = Path(path)
        checkpoint = read_json(checkpoint_path)
        if not isinstance(checkpoint, dict) or "checksum" not in checkpoint:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "checkpoint envelope is missing checksum",
                seq_id=self.seq_id,
                category="missing_checksum",
                context={"path": str(checkpoint_path)},
            )
        if verify:
            actual = sha256_json(self._checkpoint_payload(checkpoint))
            if actual != checkpoint["checksum"]:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint checksum mismatch",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="checksum_mismatch",
                    context={
                        "path": str(checkpoint_path),
                        "expected": checkpoint["checksum"],
                        "actual": actual,
                    },
                )
        if checkpoint.get("schema") != CHECKPOINT_SCHEMA:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "unsupported checkpoint schema",
                seq_id=self.seq_id,
                category="schema_mismatch",
                context={"schema": checkpoint.get("schema")},
            )
        return checkpoint

    def load_revision_checkpoints(
        self,
        revision: int,
        *,
        verify: bool = True,
    ) -> list[dict[str, Any]]:
        checkpoints = [
            self.read_checkpoint(path, verify=verify)
            for path in self.checkpoint_paths(revision)
        ]
        checkpoints.sort(key=lambda item: int(item["version"]))
        return checkpoints

    def latest_checkpoint(self, revision: int) -> dict[str, Any] | None:
        checkpoints = self.load_revision_checkpoints(revision)
        return checkpoints[-1] if checkpoints else None

    def validate_revision_chain(self, revision: int) -> list[dict[str, Any]]:
        checkpoints = self.load_revision_checkpoints(revision)
        previous_checksum: str | None = None
        previous_version: int | None = None
        expected_frame = 0
        for checkpoint in checkpoints:
            if checkpoint.get("seq_id") != self.seq_id:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint seq_id mismatch",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="seq_id_mismatch",
                )
            if int(checkpoint.get("revision", -1)) != revision:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint revision mismatch",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="revision_mismatch",
                )
            if int(checkpoint["frame_index"]) != expected_frame:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint frame chain is not contiguous",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="frame_chain_gap",
                    context={"expected_frame": expected_frame},
                )
            if int(checkpoint["version"]) <= (previous_version or 0):
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint versions are not strictly increasing",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="non_monotonic_version",
                )
            if checkpoint.get("parent_checksum") != previous_checksum:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint parent checksum mismatch",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="parent_checksum_mismatch",
                )
            if checkpoint.get("parent_version") != previous_version:
                raise TrackingError(
                    "E_CHECKPOINT_CORRUPT",
                    "checkpoint parent version mismatch",
                    seq_id=self.seq_id,
                    frame_index=checkpoint.get("frame_index"),
                    category="parent_version_mismatch",
                )
            previous_checksum = checkpoint["checksum"]
            previous_version = int(checkpoint["version"])
            expected_frame += 1
        return checkpoints

    def commit_frame(
        self,
        *,
        revision: int,
        frame_index: int,
        tracker_snapshot: dict[str, Any],
        assignments: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        frame_view: list[dict[str, Any]],
        state_distribution: dict[str, int],
        degraded_categories: list[str],
        reused_from: dict[str, Any] | None = None,
        resource_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        revision_dir = self._revision_dir(revision)
        revision_dir.mkdir(parents=True, exist_ok=True)
        prior = self.load_revision_checkpoints(revision)
        expected_frame = int(prior[-1]["frame_index"]) + 1 if prior else 0
        if frame_index != expected_frame:
            raise TrackingError(
                "E_CHECKPOINT_CORRUPT",
                "attempted to commit a frame out of sequence",
                seq_id=self.seq_id,
                frame_index=frame_index,
                category="out_of_order_commit",
                context={"expected_frame": expected_frame},
            )
        version = self._next_global_version()
        parent_version = int(prior[-1]["version"]) if prior else None
        parent_checksum = str(prior[-1]["checksum"]) if prior else None
        payload: dict[str, Any] = {
            "schema": CHECKPOINT_SCHEMA,
            "seq_id": self.seq_id,
            "revision": revision,
            "mapping_namespace": f"{self.seq_id}:r{revision}",
            "version": version,
            "frame_index": frame_index,
            "parent_version": parent_version,
            "parent_checksum": parent_checksum,
            "tracker": tracker_snapshot,
            "assignments": assignments,
            "candidates": candidates,
            "frame_view": frame_view,
            "state_distribution": state_distribution,
            "degraded_categories": sorted(set(degraded_categories)),
            "resource_metadata": resource_metadata or {},
            "reused_from": reused_from,
            "committed_complete": True,
        }
        payload["checksum"] = sha256_json(payload)
        path = revision_dir / f"{version:08d}.json"
        atomic_write_json(path, payload)
        return payload

    def copy_prefix_to_revision(
        self,
        *,
        source_revision: int,
        target_revision: int,
        frame_count: int,
    ) -> list[dict[str, Any]]:
        """Reuse a verified prefix while creating a fresh checkpoint chain.

        The copied prefix keeps the exact tracker state and frame records, but
        receives a new revision namespace and new monotonic version. The stale
        suffix is never copied.
        """

        source = self.validate_revision_chain(source_revision)
        eligible = [
            checkpoint
            for checkpoint in source
            if int(checkpoint["frame_index"]) < frame_count
        ]
        reused: list[dict[str, Any]] = []
        for checkpoint in eligible:
            assignments = []
            for row in checkpoint["assignments"]:
                copy = dict(row)
                copy["revision"] = target_revision
                assignments.append(copy)
            candidates = []
            for row in checkpoint["candidates"]:
                copy = dict(row)
                copy["revision"] = target_revision
                candidates.append(copy)
            frame_view = []
            for row in checkpoint["frame_view"]:
                copy = dict(row)
                copy["revision"] = target_revision
                frame_view.append(copy)
            tracker_snapshot = dict(checkpoint["tracker"])
            tracker_snapshot["mapping_namespace"] = f"{self.seq_id}:r{target_revision}"
            reused.append(
                self.commit_frame(
                    revision=target_revision,
                    frame_index=int(checkpoint["frame_index"]),
                    tracker_snapshot=tracker_snapshot,
                    assignments=assignments,
                    candidates=candidates,
                    frame_view=frame_view,
                    state_distribution=checkpoint["state_distribution"],
                    degraded_categories=checkpoint.get("degraded_categories", []),
                    resource_metadata=checkpoint.get("resource_metadata", {}),
                    reused_from={
                        "revision": source_revision,
                        "version": int(checkpoint["version"]),
                        "checksum": checkpoint["checksum"],
                        "reuse_kind": "unchanged_prefix",
                    },
                )
            )
        return reused

    def clear_revision(self, revision: int) -> None:
        revision_dir = self._revision_dir(revision)
        if revision_dir.exists():
            for path in sorted(revision_dir.glob("*.json")):
                path.unlink()
            try:
                revision_dir.rmdir()
            except OSError:
                pass

    def max_global_version(self) -> int:
        return self._next_global_version() - 1


def assemble_output_rows(
    checkpoints: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for checkpoint in sorted(checkpoints, key=lambda item: int(item["frame_index"])):
        rows.extend(checkpoint.get(key, []))
    return rows


def checkpoint_bytes(checkpoint: dict[str, Any]) -> bytes:
    return canonical_json(checkpoint).encode("utf-8")
