from __future__ import annotations

import base64
import hashlib
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import DigestError
from .jsonio import (
    atomic_write_json,
    canonical_json_bytes,
    digest_bytes,
    digest_value,
    read_json_checked,
)

SNAPSHOT_FORMAT = "NarrativeWorkbench.Snapshot/1"
MANIFEST_FORMAT = "NarrativeWorkbench.Manifest/1"
INTERRUPT_FORMAT = "NarrativeWorkbench.Interrupt/1"


def _safe_component(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    )[:160]


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


class SnapshotStore:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        version_id: str,
        run_id: str,
        canvas_width: int,
        canvas_height: int,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.root = self.state_dir / "snapshots"
        self.archive_root = self.state_dir / "archive"
        self.version_id = version_id
        self.run_id = run_id
        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.version_root = self.root / _safe_component(version_id)
        self.run_root = self.version_root / _safe_component(run_id)
        self.sim_dir = self.run_root / "sim"
        self.frames_dir = self.run_root / "frames"
        self.sim_manifest_path = self.sim_dir / "manifest.json"
        self.frames_manifest_path = self.frames_dir / "manifest.json"

    def prepare(self) -> list[dict[str, str]]:
        self.root.mkdir(parents=True, exist_ok=True)
        archived: list[dict[str, str]] = []
        current_name = self.version_root.name
        for child in sorted(self.root.iterdir()):
            if not child.is_dir() or child.name == current_name:
                continue
            archived.append(
                self._move_to_archive(child, reason=f"superseded-version-{child.name}")
            )
        self.sim_dir.mkdir(parents=True, exist_ok=True)
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        return archived

    def _move_to_archive(self, path: Path, *, reason: str) -> dict[str, str]:
        self.archive_root.mkdir(parents=True, exist_ok=True)
        destination = self.archive_root / (
            f"{_safe_component(reason)}-{_utc_stamp()}-{uuid.uuid4().hex[:8]}"
        )
        shutil.move(str(path), str(destination))
        return {"source": str(path), "destination": str(destination), "reason": reason}

    def archive_current_run(self, *, reason: str) -> dict[str, str] | None:
        if not self.run_root.exists():
            return None
        return self._move_to_archive(
            self.run_root, reason=f"{reason}-{self.run_id}"
        )

    def _manifest_base(self, kind: str) -> dict[str, Any]:
        return {
            "format": MANIFEST_FORMAT,
            "kind": kind,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "entries": [],
        }

    def _load_manifest(
        self, kind: str, errors: list[dict[str, Any]] | None = None
    ) -> dict[str, Any] | None:
        path = self.sim_manifest_path if kind == "sim" else self.frames_manifest_path
        if not path.exists():
            return None
        try:
            manifest = read_json_checked(path)
        except Exception as exc:
            if errors is not None:
                errors.append(
                    self._digest_error(
                        "snapshot manifest is unreadable",
                        path,
                        {"kind": kind, "reason": str(exc)},
                    )
                )
            return None
        if not isinstance(manifest, dict):
            if errors is not None:
                errors.append(
                    self._digest_error(
                        "snapshot manifest is not an object", path, {"kind": kind}
                    )
                )
            return None
        base = {key: value for key, value in manifest.items() if key != "digest"}
        required = {
            "format": MANIFEST_FORMAT,
            "kind": kind,
            "version_id": self.version_id,
            "run_id": self.run_id,
        }
        mismatch = {
            key: {"expected": value, "actual": manifest.get(key)}
            for key, value in required.items()
            if manifest.get(key) != value
        }
        if mismatch:
            if errors is not None:
                errors.append(
                    self._digest_error(
                        "snapshot manifest identity does not match",
                        path,
                        {"kind": kind, "mismatch": mismatch},
                    )
                )
            return None
        if manifest.get("digest") != digest_value(base):
            if errors is not None:
                errors.append(
                    self._digest_error(
                        "snapshot manifest digest mismatch", path, {"kind": kind}
                    )
                )
            return None
        entries = manifest.get("entries")
        if not isinstance(entries, list):
            if errors is not None:
                errors.append(
                    self._digest_error(
                        "snapshot manifest entries are malformed",
                        path,
                        {"kind": kind},
                    )
                )
            return None
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                if errors is not None:
                    errors.append(
                        self._digest_error(
                            "snapshot manifest entry is malformed",
                            path,
                            {"kind": kind, "entry_index": index},
                        )
                    )
                return None
            unit_key = entry.get("unit_key")
            if not isinstance(unit_key, str) or unit_key in seen:
                if errors is not None:
                    errors.append(
                        self._digest_error(
                            "snapshot manifest contains a missing or duplicate unit key",
                            path,
                            {"kind": kind, "entry_index": index},
                        )
                    )
                return None
            seen.add(unit_key)
        return manifest

    def _write_manifest(self, kind: str, manifest: dict[str, Any]) -> None:
        path = self.sim_manifest_path if kind == "sim" else self.frames_manifest_path
        payload = {key: value for key, value in manifest.items() if key != "digest"}
        manifest = dict(payload)
        manifest["digest"] = digest_value(payload)
        atomic_write_json(path, manifest)

    @staticmethod
    def _digest_error(
        message: str, path: Path, details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "code": "E_DIGEST",
            "message": message,
            "path": str(path),
            "details": details or {},
        }

    def _next_sequence(self, manifest: dict[str, Any] | None) -> int:
        if not manifest:
            return 1
        sequences = [
            int(entry["sequence"])
            for entry in manifest["entries"]
            if isinstance(entry.get("sequence"), int)
        ]
        return max(sequences, default=0) + 1

    def _write_snapshot(
        self,
        *,
        kind: str,
        unit_key: str,
        payload: dict[str, Any],
        entry_count: int,
        sequence: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        manifest_path = (
            self.sim_manifest_path if kind == "sim" else self.frames_manifest_path
        )
        manifest = self._load_manifest(kind)
        if manifest is None and manifest_path.exists():
            raise DigestError(
                "refusing to overwrite an invalid snapshot manifest",
                {"kind": kind, "path": str(manifest_path)},
            )
        if manifest is None:
            manifest = self._manifest_base(kind)
        existing = next(
            (
                entry
                for entry in manifest["entries"]
                if entry.get("unit_key") == unit_key
            ),
            None,
        )
        if sequence is None:
            if existing is not None:
                sequence = int(existing["sequence"])
            else:
                sequence = self._next_sequence(manifest)
        safe_key = _safe_component(unit_key)
        if kind == "sim":
            file_name = f"sim-{sequence:08d}-{safe_key}.json"
            directory = self.sim_dir
        else:
            file_name = f"frame-{sequence:08d}-{safe_key}.json"
            directory = self.frames_dir
        path = directory / file_name
        envelope_base = {
            "format": SNAPSHOT_FORMAT,
            "kind": kind,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "unit_key": unit_key,
            "sequence": sequence,
            "entry_count": entry_count,
            "payload": payload,
        }
        if extra:
            envelope_base.update(extra)
        envelope = dict(envelope_base)
        envelope["digest"] = digest_value(envelope_base)
        atomic_write_json(path, envelope)
        entry = {
            "unit_key": unit_key,
            "sequence": sequence,
            "file": file_name,
            "digest": envelope["digest"],
            "entry_count": entry_count,
        }
        if extra:
            for key, value in extra.items():
                if key in {"scene_id", "block_index", "start_frame", "end_frame"}:
                    entry[key] = value
        if existing is not None:
            entry_index = manifest["entries"].index(existing)
            manifest["entries"][entry_index] = entry
        else:
            manifest["entries"].append(entry)
        manifest["entries"].sort(key=lambda item: int(item["sequence"]))
        self._write_manifest(kind, manifest)
        return envelope

    def save_sim(self, payload: dict[str, Any], *, sequence: int | None = None) -> dict[str, Any]:
        ledger = payload.get("ledger")
        if not isinstance(ledger, list):
            raise DigestError("simulation payload has no ledger")
        if sequence is None:
            manifest = self._load_manifest("sim")
            sequence = self._next_sequence(manifest)
        return self._write_snapshot(
            kind="sim",
            unit_key=f"sim-{sequence:08d}",
            payload=payload,
            entry_count=len(ledger),
            sequence=sequence,
        )

    def save_frame_block(
        self,
        *,
        unit_key: str,
        payload: dict[str, Any],
        scene_id: str,
        block_index: int,
        start_frame: int,
        end_frame: int,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        frames = payload.get("frames")
        if not isinstance(frames, list):
            raise DigestError("frame payload has no frame array")
        return self._write_snapshot(
            kind="frames",
            unit_key=unit_key,
            payload=payload,
            entry_count=len(frames),
            sequence=sequence,
            extra={
                "scene_id": scene_id,
                "block_index": block_index,
                "start_frame": start_frame,
                "end_frame": end_frame,
            },
        )

    def _read_snapshot(
        self,
        *,
        kind: str,
        entry: dict[str, Any],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        directory = self.sim_dir if kind == "sim" else self.frames_dir
        file_name = entry.get("file")
        if not isinstance(file_name, str):
            errors.append(
                self._digest_error(
                    "snapshot manifest entry has no file name",
                    directory,
                    {"kind": kind, "unit_key": entry.get("unit_key")},
                )
            )
            return None
        path = directory / file_name
        if not path.exists():
            errors.append(
                self._digest_error(
                    "snapshot file referenced by manifest is missing",
                    path,
                    {"kind": kind, "unit_key": entry.get("unit_key")},
                )
            )
            return None
        try:
            envelope = read_json_checked(path)
        except Exception as exc:
            errors.append(
                self._digest_error(
                    "snapshot file is unreadable",
                    path,
                    {"kind": kind, "reason": str(exc)},
                )
            )
            return None
        if not isinstance(envelope, dict):
            errors.append(
                self._digest_error(
                    "snapshot envelope is not an object", path, {"kind": kind}
                )
            )
            return None
        base = {key: value for key, value in envelope.items() if key != "digest"}
        if envelope.get("digest") != digest_value(base):
            errors.append(
                self._digest_error(
                    "snapshot content digest mismatch",
                    path,
                    {"kind": kind, "unit_key": entry.get("unit_key")},
                )
            )
            return None
        if envelope.get("digest") != entry.get("digest"):
            errors.append(
                self._digest_error(
                    "snapshot digest does not match the manifest record",
                    path,
                    {"kind": kind, "unit_key": entry.get("unit_key")},
                )
            )
            return None
        required = {
            "format": SNAPSHOT_FORMAT,
            "kind": kind,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "unit_key": entry.get("unit_key"),
            "sequence": entry.get("sequence"),
            "entry_count": entry.get("entry_count"),
        }
        mismatch = {
            key: {"expected": value, "actual": envelope.get(key)}
            for key, value in required.items()
            if envelope.get(key) != value
        }
        if mismatch:
            errors.append(
                self._digest_error(
                    "snapshot identity or count does not match the manifest",
                    path,
                    {"kind": kind, "mismatch": mismatch},
                )
            )
            return None
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            errors.append(
                self._digest_error(
                    "snapshot payload is not an object", path, {"kind": kind}
                )
            )
            return None
        if kind == "sim":
            ledger = payload.get("ledger")
            if not isinstance(ledger, list) or len(ledger) != envelope["entry_count"]:
                errors.append(
                    self._digest_error(
                        "simulation snapshot entry count does not match its ledger",
                        path,
                        {
                            "declared": envelope["entry_count"],
                            "actual": len(ledger) if isinstance(ledger, list) else None,
                        },
                    )
                )
                return None
        else:
            frames = payload.get("frames")
            if not isinstance(frames, list) or len(frames) != envelope["entry_count"]:
                errors.append(
                    self._digest_error(
                        "frame snapshot entry count does not match its frame array",
                        path,
                        {
                            "declared": envelope["entry_count"],
                            "actual": len(frames) if isinstance(frames, list) else None,
                        },
                    )
                )
                return None
            if not self._validate_frame_pixels(frames, path, errors):
                return None
        return payload

    def _validate_frame_pixels(
        self, frames: list[Any], path: Path, errors: list[dict[str, Any]]
    ) -> bool:
        expected_bytes = self.canvas_width * self.canvas_height
        for index, frame in enumerate(frames):
            if not isinstance(frame, dict):
                errors.append(
                    self._digest_error(
                        "frame entry is not an object",
                        path,
                        {"frame_index": index},
                    )
                )
                return False
            width = frame.get("width")
            height = frame.get("height")
            encoded = frame.get("pixels_base64")
            declared_digest = frame.get("pixels_sha256")
            if (
                width != self.canvas_width
                or height != self.canvas_height
                or not isinstance(encoded, str)
                or not isinstance(declared_digest, str)
            ):
                errors.append(
                    self._digest_error(
                        "frame pixel metadata is malformed",
                        path,
                        {"frame_index": index},
                    )
                )
                return False
            try:
                pixels = base64.b64decode(encoded, validate=True)
            except Exception:
                errors.append(
                    self._digest_error(
                        "frame pixel payload is not valid base64",
                        path,
                        {"frame_index": index},
                    )
                )
                return False
            if len(pixels) != expected_bytes:
                errors.append(
                    self._digest_error(
                        "frame pixel payload has the wrong byte count",
                        path,
                        {
                            "frame_index": index,
                            "expected": expected_bytes,
                            "actual": len(pixels),
                        },
                    )
                )
                return False
            actual_digest = hashlib.sha256(pixels).hexdigest()
            if actual_digest != declared_digest:
                errors.append(
                    self._digest_error(
                        "frame pixel digest mismatch",
                        path,
                        {"frame_index": index},
                    )
                )
                return False
        return True

    def recover(
        self, *, archive_version_events: list[dict[str, str]] | None = None
    ) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        sim_manifest = self._load_manifest("sim", errors)
        frame_manifest = self._load_manifest("frames", errors)
        valid_sim: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
        if sim_manifest:
            for entry in sim_manifest["entries"]:
                payload = self._read_snapshot(
                    kind="sim", entry=entry, errors=errors
                )
                if payload is not None:
                    valid_sim.append(
                        (int(entry["sequence"]), payload, entry)
                    )
        valid_frames: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        if frame_manifest:
            for entry in frame_manifest["entries"]:
                payload = self._read_snapshot(
                    kind="frames", entry=entry, errors=errors
                )
                if payload is not None:
                    valid_frames[str(entry["unit_key"])] = (payload, entry)

        referenced_sim = {
            entry.get("file") for entry in (sim_manifest or {}).get("entries", [])
        }
        referenced_frames = {
            entry.get("file") for entry in (frame_manifest or {}).get("entries", [])
        }
        for directory, referenced, kind in (
            (self.sim_dir, referenced_sim, "sim"),
            (self.frames_dir, referenced_frames, "frames"),
        ):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                if path.name == "manifest.json" or path.name in referenced:
                    continue
                errors.append(
                    self._digest_error(
                        "snapshot file is not referenced by a valid manifest",
                        path,
                        {"kind": kind},
                    )
                )

        latest_sim = max(valid_sim, key=lambda item: item[0]) if valid_sim else None
        frame_count = sum(
            len(payload.get("frames", []))
            for payload, _entry in valid_frames.values()
        )
        report = {
            "ok": not errors,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "archive_events": archive_version_events or [],
            "sim": {
                "valid_snapshot_count": len(valid_sim),
                "used_sequence": latest_sim[0] if latest_sim else None,
                "used_entry_count": (
                    len(latest_sim[1].get("ledger", [])) if latest_sim else 0
                ),
                "has_complete_snapshot": bool(
                    latest_sim
                    and latest_sim[1].get("progress", {}).get("status") == "complete"
                ),
            },
            "frames": {
                "valid_block_count": len(valid_frames),
                "recovered_frame_count": frame_count,
                "unit_keys": sorted(valid_frames),
            },
            "errors": errors,
        }
        return {
            "report": report,
            "sim_payload": latest_sim[1] if latest_sim else None,
            "sim_entry": latest_sim[2] if latest_sim else None,
            "frame_blocks": {
                key: value[0] for key, value in valid_frames.items()
            },
            "frame_entries": {
                key: value[1] for key, value in valid_frames.items()
            },
        }

    def read_interrupt(self) -> dict[str, Any] | None:
        path = self.state_dir / "interrupt.json"
        if not path.exists():
            return None
        try:
            payload = read_json_checked(path)
        except Exception:
            return {
                "code": "E_DIGEST",
                "message": "interrupt marker is unreadable",
                "path": str(path),
            }
        if not isinstance(payload, dict) or payload.get("format") != INTERRUPT_FORMAT:
            return {
                "code": "E_DIGEST",
                "message": "interrupt marker has an unsupported format",
                "path": str(path),
            }
        return payload

    def write_interrupt(
        self,
        *,
        signal_name: str,
        boundary: str,
        exit_code: int,
        frame_index: int | None,
        scene_id: str | None,
        snapshot_sequence: int | None,
        test_injected: bool,
    ) -> dict[str, Any]:
        marker = {
            "format": INTERRUPT_FORMAT,
            "code": "E_INTERRUPTED",
            "signal": signal_name,
            "boundary": boundary,
            "exit_code": exit_code,
            "frame_index": frame_index,
            "scene_id": scene_id,
            "snapshot_sequence": snapshot_sequence,
            "test_injected": test_injected,
            "written_at_utc": datetime.now(timezone.utc).isoformat(),
            "version_id": self.version_id,
            "run_id": self.run_id,
        }
        atomic_write_json(self.state_dir / "interrupt.json", marker)
        return marker

    def clear_interrupt(self) -> None:
        (self.state_dir / "interrupt.json").unlink(missing_ok=True)

    def current_sim_sequence(self) -> int:
        manifest = self._load_manifest("sim")
        return max(
            (
                int(entry["sequence"])
                for entry in (manifest or {}).get("entries", [])
                if isinstance(entry.get("sequence"), int)
            ),
            default=0,
        )

    def fingerprint(self) -> str:
        manifest = self._load_manifest("sim")
        if not manifest:
            return digest_bytes(b"empty")
        return digest_value(manifest["entries"])

