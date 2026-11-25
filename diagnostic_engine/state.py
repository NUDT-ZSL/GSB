"""Persistent four-stage snapshots and restart recovery."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .cache import CommitLock
from .hashing import canonical_json, sha256_json


STAGES = ["preprocess", "cross_standardize", "reduce_decay", "predict_error"]


class StateManager:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshots = self.root / "snapshots"
        self.incremental = self.root / "incremental"
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.incremental.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / "commit.lock"

    def window_dir(self, window_id: str, profile: str) -> Path:
        safe_window = window_id.replace("/", "_")
        path = self.snapshots / f"{safe_window}__{profile}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _snapshot_path(self, window_id: str, profile: str, version: int) -> Path:
        return self.window_dir(window_id, profile) / f"snapshot-{version:08d}.json"

    def latest_path(self, window_id: str, profile: str) -> Path:
        return self.window_dir(window_id, profile) / "latest.json"

    def list_snapshots(self, window_id: str, profile: str) -> list[Path]:
        return sorted(self.window_dir(window_id, profile).glob("snapshot-*.json"))

    def all_snapshot_files(self) -> list[Path]:
        return sorted(self.snapshots.glob("*/snapshot-*.json"))

    def max_version(self, window_id: str, profile: str) -> int:
        versions = []
        for path in self.list_snapshots(window_id, profile):
            try:
                versions.append(int(path.stem.split("-")[1]))
            except (IndexError, ValueError):
                continue
        return max(versions, default=0)

    def commit_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        window_id = snapshot["window_id"]
        profile = snapshot["precision_profile"]
        version = int(snapshot.get("version") or self.max_version(window_id, profile) + 1)
        if version <= self.max_version(window_id, profile):
            version = self.max_version(window_id, profile) + 1
        snapshot = dict(snapshot)
        snapshot["version"] = version
        snapshot["monotonic_key"] = f"{window_id}:{profile}:{version}"
        checksum_payload = {k: v for k, v in snapshot.items() if k != "checksum"}
        snapshot["checksum"] = sha256_json(checksum_payload)

        target = self._snapshot_path(window_id, profile, version)
        latest = self.latest_path(window_id, profile)
        with CommitLock(self.lock_path):
            tmp_target = target.with_name(f".{target.name}.tmp-{os.getpid()}")
            tmp_target.write_text(canonical_json(snapshot) + "\n", encoding="utf-8")
            tmp_target.replace(target)
            tmp_latest = latest.with_name(f".{latest.name}.tmp-{os.getpid()}")
            tmp_latest.write_text(canonical_json({"path": target.name, "version": version}) + "\n", encoding="utf-8")
            tmp_latest.replace(latest)
        return snapshot

    def load_latest(self, window_id: str, profile: str) -> dict[str, Any] | None:
        latest = self.latest_path(window_id, profile)
        if latest.exists():
            pointer = json.loads(latest.read_text(encoding="utf-8"))
            path = latest.parent / pointer["path"]
        else:
            files = self.list_snapshots(window_id, profile)
            if not files:
                return None
            path = files[-1]
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        expected = snapshot.pop("checksum", None)
        actual = sha256_json(snapshot)
        snapshot["checksum"] = expected
        if expected != actual:
            from .errors import DiagnosticError

            raise DiagnosticError("E_STATE_CORRUPT", "stage snapshot checksum mismatch", path=str(path))
        return snapshot

    def committed_cache_keys(self) -> set[str]:
        keys: set[str] = set()
        for path in self.all_snapshot_files():
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            keys.update(snapshot.get("cache_keys", {}).values())
        return keys

    def cleanup_temporary(self) -> int:
        removed = 0
        for root in (self.root,):
            for path in root.rglob("*.tmp-*"):
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed

    def write_exit_report(self, report: dict[str, Any]) -> Path:
        path = self.root / "exit_report.json"
        tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        tmp.write_text(canonical_json(report) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def commit_incremental_manifest(self, manifest: dict[str, Any]) -> Path:
        version_path = self.incremental / "latest.json"
        with CommitLock(self.lock_path):
            existing = []
            history_path = self.incremental / "history.jsonl"
            if history_path.exists():
                existing = history_path.read_text(encoding="utf-8").splitlines()
            version = len(existing) + 1
            manifest = dict(manifest)
            manifest["incremental_version"] = version
            manifest["checksum"] = sha256_json({k: v for k, v in manifest.items() if k != "checksum"})
            entry = canonical_json(manifest)
            with open(history_path, "a", encoding="utf-8") as handle:
                handle.write(entry + "\n")
            tmp = version_path.with_name(f".latest.json.tmp-{os.getpid()}")
            tmp.write_text(entry + "\n", encoding="utf-8")
            tmp.replace(version_path)
        return version_path
