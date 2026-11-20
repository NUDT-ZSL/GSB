"""Immutable, checksummed, atomically switched workbench snapshots."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from .bridge import parse_bridge_text
from .errors import format_corrupt, inconsistent, version_skew
from .models import BridgeResource, RuleSet
from .rules import (
    ruleset_checksum,
    ruleset_from_semantic_dict,
    ruleset_semantic_dict,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    checksum_object,
    read_json,
    stable_json_bytes,
)


@dataclass(frozen=True)
class SnapshotView:
    commit_id: str
    commit: Mapping[str, Any]
    rules: RuleSet
    bridge: BridgeResource
    rule_manifest: Mapping[str, Any]
    bridge_manifest: Mapping[str, Any]
    index_manifest: Mapping[str, Any]

    def partition_loader(self) -> Callable[[str], Mapping[str, Any]]:
        paths = {
            item["lang"]: item["path"]
            for item in self.index_manifest["partitions"]
        }
        checksums = {
            item["lang"]: item["checksum"]
            for item in self.index_manifest["partitions"]
        }

        def load(lang: str) -> Mapping[str, Any]:
            if lang not in paths:
                raise format_corrupt(
                    "partition is missing from index snapshot",
                    partition_lang=lang,
                )
            path = Path(str(self.commit["_index_snapshot_dir"])) / paths[lang]
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise version_skew(
                    "partition cannot be read",
                    partition_lang=lang,
                    path=str(path),
                    reason=str(exc),
                ) from exc
            if checksum_from_bytes(data) != checksums[lang]:
                raise version_skew(
                    "partition checksum mismatch",
                    partition_lang=lang,
                    expected=checksums[lang],
                    actual=checksum_from_bytes(data),
                )
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise format_corrupt(
                    "partition JSON is corrupt",
                    partition_lang=lang,
                    path=str(path),
                    line=exc.lineno,
                    column=exc.colno,
                ) from exc
            return payload

        return load


def checksum_from_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class SnapshotStore:
    """Root layout:

    rules/vN/manifest.json
    bridge/vN/manifest.json
    index/vN/manifest.json + partitions/*.json
    commits/<commit-id>.json
    CURRENT
    builds/<build-id>/
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.rules_root = self.root / "rules"
        self.bridge_root = self.root / "bridge"
        self.index_root = self.root / "index"
        self.commits_root = self.root / "commits"
        self.builds_root = self.root / "builds"
        self.current_path = self.root / "CURRENT"
        for path in (
            self.rules_root,
            self.bridge_root,
            self.index_root,
            self.commits_root,
            self.builds_root,
        ):
            path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ rules
    def save_rules_snapshot(self, ruleset: RuleSet, version: int) -> dict[str, Any]:
        _require_version(version)
        target = self.rules_root / f"v{version}"
        checksum = ruleset_checksum(ruleset)
        payload = ruleset_semantic_dict(ruleset)
        manifest = {
            "kind": "rules",
            "version": version,
            "checksum": checksum,
            "format_version": ruleset.format_version,
            "rule_count": len(ruleset.rules),
            "rules_payload": payload,
        }
        self._save_immutable_manifest(target, manifest)
        return manifest

    def load_rules_snapshot(self, version: int) -> tuple[dict[str, Any], RuleSet]:
        path = self.rules_root / f"v{version}" / "manifest.json"
        manifest = self._read_manifest(path)
        if manifest.get("kind") != "rules":
            raise version_skew("rules snapshot kind mismatch", path=str(path))
        payload = manifest.get("rules_payload")
        if not isinstance(payload, dict):
            raise format_corrupt("rules snapshot payload is invalid", path=str(path))
        ruleset = ruleset_from_semantic_dict(
            payload,
            format_version=manifest.get("format_version", "v3"),
        )
        actual = ruleset_checksum(ruleset)
        if actual != manifest.get("checksum"):
            raise version_skew(
                "rules snapshot checksum mismatch",
                version=version,
                expected=manifest.get("checksum"),
                actual=actual,
            )
        return manifest, ruleset

    # ----------------------------------------------------------------- bridge
    def save_bridge_snapshot(
        self,
        bridge: BridgeResource,
        version: int | None = None,
    ) -> dict[str, Any]:
        snapshot_version = bridge.version if version is None else version
        _require_version(snapshot_version)
        if snapshot_version != bridge.version:
            raise version_skew(
                "bridge snapshot version differs from resource version",
                snapshot_version=snapshot_version,
                resource_version=bridge.version,
            )
        target = self.bridge_root / f"v{snapshot_version}"
        manifest = {
            "kind": "bridge",
            "version": snapshot_version,
            "checksum": bridge.checksum,
            "resource": bridge.to_dict(include_checksum=False),
        }
        self._save_immutable_manifest(target, manifest)
        return manifest

    def load_bridge_snapshot(self, version: int) -> tuple[dict[str, Any], BridgeResource]:
        path = self.bridge_root / f"v{version}" / "manifest.json"
        manifest = self._read_manifest(path)
        if manifest.get("kind") != "bridge":
            raise version_skew("bridge snapshot kind mismatch", path=str(path))
        resource_raw = manifest.get("resource")
        if not isinstance(resource_raw, dict):
            raise format_corrupt("bridge snapshot resource is invalid", path=str(path))
        wrapped = {"resource": resource_raw, "checksum": manifest.get("checksum")}
        bridge = parse_bridge_text(
            stable_json_bytes(wrapped).decode("utf-8"),
            source=str(path),
        )
        if bridge.checksum != manifest.get("checksum"):
            raise version_skew(
                "bridge snapshot checksum mismatch",
                version=version,
                expected=manifest.get("checksum"),
                actual=bridge.checksum,
            )
        return manifest, bridge

    # ------------------------------------------------------------------ index
    def save_index_snapshot(
        self,
        *,
        index_version: int,
        partitions: Mapping[str, Mapping[str, Any]],
        rule_manifest: Mapping[str, Any],
        bridge_manifest: Mapping[str, Any],
        docs_checksum: str,
        degraded: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        _require_version(index_version)
        target = self.index_root / f"v{index_version}"
        payloads: dict[str, bytes] = {}
        partition_entries: list[dict[str, Any]] = []
        total_postings = 0
        total_bytes = 0
        for lang in sorted(partitions):
            partition = dict(partitions[lang])
            if partition.get("partition_lang") != lang:
                raise format_corrupt(
                    "partition language mismatch",
                    expected=lang,
                    actual=partition.get("partition_lang"),
                )
            if partition.get("bridge_version") != bridge_manifest["version"]:
                raise version_skew(
                    "partition bridge version is stale before snapshot",
                    partition_lang=lang,
                    expected=bridge_manifest["version"],
                    actual=partition.get("bridge_version"),
                )
            if partition.get("bridge_checksum") != bridge_manifest["checksum"]:
                raise version_skew(
                    "partition bridge checksum is stale before snapshot",
                    partition_lang=lang,
                )
            data = stable_json_bytes(partition)
            relative = f"partitions/{quote(lang, safe='')}.json"
            payloads[relative] = data
            posting_count = len(partition.get("postings", []))
            total_postings += posting_count
            total_bytes += len(data)
            partition_entries.append(
                {
                    "lang": lang,
                    "path": relative,
                    "checksum": checksum_from_bytes(data),
                    "posting_count": posting_count,
                    "decoded_bytes": len(data),
                }
            )

        core = {
            "kind": "index",
            "version": index_version,
            "rule_version": rule_manifest["version"],
            "rule_checksum": rule_manifest["checksum"],
            "bridge_version": bridge_manifest["version"],
            "bridge_checksum": bridge_manifest["checksum"],
            "docs_checksum": docs_checksum,
            "posting_count": total_postings,
            "decoded_bytes": total_bytes,
            "degraded": sorted(set(degraded)),
            "partitions": sorted(partition_entries, key=lambda item: item["lang"]),
        }
        manifest = dict(core)
        manifest["checksum"] = checksum_object(core)
        self._save_immutable_index(target, manifest, payloads)
        return manifest

    def load_index_snapshot(
        self,
        version: int,
    ) -> tuple[dict[str, Any], dict[str, Mapping[str, Any]]]:
        manifest = self.load_index_manifest(version)
        path = self.index_root / f"v{version}" / "manifest.json"
        partitions: dict[str, Mapping[str, Any]] = {}
        for entry in manifest["partitions"]:
            partition_path = path.parent / entry["path"]
            try:
                data = partition_path.read_bytes()
            except OSError as exc:
                raise format_corrupt(
                    "index partition cannot be read",
                    path=str(partition_path),
                    reason=str(exc),
                ) from exc
            actual = checksum_from_bytes(data)
            if actual != entry["checksum"]:
                raise version_skew(
                    "index partition checksum mismatch",
                    partition_lang=entry["lang"],
                    expected=entry["checksum"],
                    actual=actual,
                )
            payload = json.loads(data)
            if payload.get("partition_lang") != entry["lang"]:
                raise version_skew(
                    "index partition language mismatch",
                    expected=entry["lang"],
                    actual=payload.get("partition_lang"),
                )
            partitions[entry["lang"]] = payload
        return manifest, partitions

    def load_index_manifest(self, version: int) -> dict[str, Any]:
        path = self.index_root / f"v{version}" / "manifest.json"
        manifest = self._read_manifest(path)
        _verify_index_manifest(manifest, path=path)
        return manifest

    # ----------------------------------------------------------------- commit
    def commit(
        self,
        *,
        rule_manifest: Mapping[str, Any],
        bridge_manifest: Mapping[str, Any],
        index_manifest: Mapping[str, Any],
        affected_partitions: list[str] | tuple[str, ...],
        reused_partitions: list[str] | tuple[str, ...],
        recomputed_partitions: list[str] | tuple[str, ...],
        degraded: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        affected = set(affected_partitions)
        reused = set(reused_partitions)
        recomputed = set(recomputed_partitions)
        if reused & recomputed:
            raise inconsistent(
                "reused and recomputed partitions overlap",
                overlap=sorted(reused & recomputed),
            )
        if not affected <= (reused | recomputed):
            raise inconsistent(
                "affected partitions are not covered by reuse/recompute sets",
                uncovered=sorted(affected - (reused | recomputed)),
            )
        if index_manifest.get("rule_version") != rule_manifest.get("version"):
            raise version_skew("index and rule versions differ")
        if index_manifest.get("rule_checksum") != rule_manifest.get("checksum"):
            raise version_skew("index and rule checksums differ")
        if index_manifest.get("bridge_version") != bridge_manifest.get("version"):
            raise version_skew("index and bridge versions differ")
        if index_manifest.get("bridge_checksum") != bridge_manifest.get("checksum"):
            raise version_skew("index and bridge checksums differ")

        previous = self.load_current_optional()
        if previous is not None:
            self._check_monotonic(previous, rule_manifest, bridge_manifest, index_manifest)

        core = {
            "kind": "snapshot_commit",
            "snapshot_mode": "full_replace",
            "rule_version": rule_manifest["version"],
            "rule_checksum": rule_manifest["checksum"],
            "bridge_version": bridge_manifest["version"],
            "bridge_checksum": bridge_manifest["checksum"],
            "index_version": index_manifest["version"],
            "index_checksum": index_manifest["checksum"],
            "affected_partitions": sorted(affected),
            "reused_partitions": sorted(reused),
            "recomputed_partitions": sorted(recomputed),
            "degraded": sorted(set(degraded)),
        }
        commit_id = checksum_object(core)[:32]
        record = {"commit_id": commit_id, **core}
        commit_path = self.commits_root / f"{commit_id}.json"
        if commit_path.exists():
            existing = self._read_manifest(commit_path)
            if existing != record:
                raise version_skew(
                    "commit hash collision",
                    commit_id=commit_id,
                )
        else:
            atomic_write_json(commit_path, record)
        atomic_write_bytes(
            self.current_path,
            (commit_id + "\n").encode("ascii"),
        )
        return record

    def load_current(self) -> SnapshotView:
        view = self.load_current_optional()
        if view is None:
            raise version_skew("no committed snapshot exists")
        return view

    def load_current_optional(self) -> SnapshotView | None:
        if not self.current_path.exists():
            return None
        try:
            commit_id = self.current_path.read_text(encoding="ascii").strip()
        except OSError as exc:
            raise format_corrupt(
                "cannot read CURRENT pointer",
                path=str(self.current_path),
                reason=str(exc),
            ) from exc
        if not commit_id:
            raise format_corrupt(
                "CURRENT pointer is empty",
                path=str(self.current_path),
            )
        return self.load_commit(commit_id)

    def load_commit(
        self,
        commit_id: str,
        *,
        expected_rule_version: int | None = None,
        expected_bridge_version: int | None = None,
        expected_index_version: int | None = None,
    ) -> SnapshotView:
        commit_path = self.commits_root / f"{commit_id}.json"
        commit = self._read_manifest(commit_path)
        if commit.get("kind") != "snapshot_commit":
            raise version_skew("invalid commit record kind", commit_id=commit_id)
        if commit.get("commit_id") != commit_id:
            raise version_skew(
                "commit id does not match file name",
                path=str(commit_path),
            )
        commit_core = {
            key: value
            for key, value in commit.items()
            if key != "commit_id"
        }
        if checksum_object(commit_core)[:32] != commit_id:
            raise version_skew(
                "commit record checksum does not match commit id",
                commit_id=commit_id,
                path=str(commit_path),
            )
        if expected_rule_version is not None and (
            commit.get("rule_version") != expected_rule_version
        ):
            raise version_skew(
                "requested rule version is not active",
                requested=expected_rule_version,
                active=commit.get("rule_version"),
            )
        if expected_bridge_version is not None and (
            commit.get("bridge_version") != expected_bridge_version
        ):
            raise version_skew(
                "requested bridge version is not active",
                requested=expected_bridge_version,
                active=commit.get("bridge_version"),
            )
        if expected_index_version is not None and (
            commit.get("index_version") != expected_index_version
        ):
            raise version_skew(
                "requested index version is not active",
                requested=expected_index_version,
                active=commit.get("index_version"),
            )

        rule_manifest, rules = self.load_rules_snapshot(commit["rule_version"])
        bridge_manifest, bridge = self.load_bridge_snapshot(commit["bridge_version"])
        index_manifest = self.load_index_manifest(commit["index_version"])
        self._cross_check(
            commit,
            rule_manifest,
            bridge_manifest,
            index_manifest,
        )
        mutable_commit = dict(commit)
        mutable_commit["_index_snapshot_dir"] = str(
            self.index_root / f"v{commit['index_version']}"
        )
        return SnapshotView(
            commit_id=commit_id,
            commit=mutable_commit,
            rules=rules,
            bridge=bridge,
            rule_manifest=rule_manifest,
            bridge_manifest=bridge_manifest,
            index_manifest=index_manifest,
        )

    def verify_commit(self, view: SnapshotView | None = None) -> dict[str, Any]:
        active = view or self.load_current()
        self._cross_check(
            active.commit,
            active.rule_manifest,
            active.bridge_manifest,
            active.index_manifest,
        )
        return {
            "ok": True,
            "commit_id": active.commit_id,
            "rule_version": active.commit["rule_version"],
            "bridge_version": active.commit["bridge_version"],
            "index_version": active.commit["index_version"],
        }

    # --------------------------------------------------------------- builds
    def build_state_path(self, build_id: str) -> Path:
        return self.builds_root / build_id / "state.json"

    def load_build_state(self, build_id: str) -> dict[str, Any] | None:
        path = self.build_state_path(build_id)
        if not path.exists():
            return None
        return self._read_manifest(path)

    def save_build_state(self, build_id: str, state: Mapping[str, Any]) -> None:
        atomic_write_json(self.build_state_path(build_id), dict(state))

    def build_partition_path(self, build_id: str, lang: str) -> Path:
        return self.builds_root / build_id / "partitions" / f"{quote(lang, safe='')}.json"

    def write_build_partition(self, build_id: str, lang: str, data: bytes) -> str:
        path = self.build_partition_path(build_id, lang)
        atomic_write_bytes(path, data)
        return checksum_from_bytes(data)

    def read_build_partition(self, build_id: str, lang: str) -> dict[str, Any]:
        path = self.build_partition_path(build_id, lang)
        data = path.read_bytes()
        return json.loads(data)

    # -------------------------------------------------------------- internals
    def _save_immutable_manifest(self, target: Path, manifest: Mapping[str, Any]) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self._read_manifest(target / "manifest.json")
            if dict(existing) != dict(manifest):
                raise version_skew(
                    "version already exists with different content",
                    path=str(target),
                    existing_checksum=existing.get("checksum"),
                    incoming_checksum=manifest.get("checksum"),
                )
            return
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
        )
        try:
            atomic_write_json(temporary / "manifest.json", dict(manifest))
            try:
                os.replace(temporary, target)
            except OSError:
                if target.exists():
                    existing = self._read_manifest(target / "manifest.json")
                    if dict(existing) == dict(manifest):
                        return
                raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _save_immutable_index(
        self,
        target: Path,
        manifest: Mapping[str, Any],
        payloads: Mapping[str, bytes],
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing = self._read_manifest(target / "manifest.json")
            if dict(existing) != dict(manifest):
                raise version_skew(
                    "index version already exists with different content",
                    path=str(target),
                    existing_checksum=existing.get("checksum"),
                    incoming_checksum=manifest.get("checksum"),
                )
            return
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent)
        )
        try:
            for relative, data in sorted(payloads.items()):
                destination = temporary / relative
                atomic_write_bytes(destination, data)
            atomic_write_json(temporary / "manifest.json", dict(manifest))
            try:
                os.replace(temporary, target)
            except OSError:
                if target.exists():
                    existing = self._read_manifest(target / "manifest.json")
                    if dict(existing) == dict(manifest):
                        return
                raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    @staticmethod
    def _read_manifest(path: Path) -> dict[str, Any]:
        value = read_json(path)
        if not isinstance(value, dict):
            raise format_corrupt("snapshot manifest must be an object", path=str(path))
        return value

    @staticmethod
    def _cross_check(
        commit: Mapping[str, Any],
        rule_manifest: Mapping[str, Any],
        bridge_manifest: Mapping[str, Any],
        index_manifest: Mapping[str, Any],
    ) -> None:
        pairs = [
            ("rule_version", commit.get("rule_version"), rule_manifest.get("version")),
            ("rule_checksum", commit.get("rule_checksum"), rule_manifest.get("checksum")),
            ("bridge_version", commit.get("bridge_version"), bridge_manifest.get("version")),
            ("bridge_checksum", commit.get("bridge_checksum"), bridge_manifest.get("checksum")),
            ("index_version", commit.get("index_version"), index_manifest.get("version")),
            ("index_checksum", commit.get("index_checksum"), index_manifest.get("checksum")),
            ("index.rule_version", commit.get("rule_version"), index_manifest.get("rule_version")),
            ("index.rule_checksum", commit.get("rule_checksum"), index_manifest.get("rule_checksum")),
            ("index.bridge_version", commit.get("bridge_version"), index_manifest.get("bridge_version")),
            ("index.bridge_checksum", commit.get("bridge_checksum"), index_manifest.get("bridge_checksum")),
        ]
        for field, expected, actual in pairs:
            if expected != actual:
                raise version_skew(
                    "cross-snapshot version/checksum mismatch",
                    field=field,
                    expected=expected,
                    actual=actual,
                )

    def _check_monotonic(
        self,
        previous: SnapshotView,
        rule_manifest: Mapping[str, Any],
        bridge_manifest: Mapping[str, Any],
        index_manifest: Mapping[str, Any],
    ) -> None:
        components = (
            (
                "rule",
                previous.commit["rule_version"],
                previous.commit["rule_checksum"],
                rule_manifest["version"],
                rule_manifest["checksum"],
            ),
            (
                "bridge",
                previous.commit["bridge_version"],
                previous.commit["bridge_checksum"],
                bridge_manifest["version"],
                bridge_manifest["checksum"],
            ),
            (
                "index",
                previous.commit["index_version"],
                previous.commit["index_checksum"],
                index_manifest["version"],
                index_manifest["checksum"],
            ),
        )
        for name, old_version, old_checksum, new_version, new_checksum in components:
            if new_version < old_version:
                raise version_skew(
                    "snapshot version moved backwards",
                    component=name,
                    previous=old_version,
                    current=new_version,
                )
            if new_version == old_version and new_checksum != old_checksum:
                raise version_skew(
                    "snapshot content changed without a version increment",
                    component=name,
                    version=new_version,
                    previous_checksum=old_checksum,
                    current_checksum=new_checksum,
                )


def _verify_index_manifest(manifest: Mapping[str, Any], *, path: Path) -> None:
    if manifest.get("kind") != "index":
        raise version_skew("index snapshot kind mismatch", path=str(path))
    core = {
        key: value
        for key, value in manifest.items()
        if key != "checksum"
    }
    actual = checksum_object(core)
    if actual != manifest.get("checksum"):
        raise version_skew(
            "index manifest checksum mismatch",
            path=str(path),
            expected=manifest.get("checksum"),
            actual=actual,
        )


def _require_version(version: int) -> None:
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise version_skew("snapshot version must be a positive integer", version=version)
