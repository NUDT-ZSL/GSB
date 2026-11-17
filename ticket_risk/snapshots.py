"""Versioned, atomically replaced stage snapshots and recovery validation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .errors import E_CONFIG_MISMATCH, E_SNAPSHOT_INVALID, Failure, SnapshotError
from .state import STAGES, RuntimeState
from .util import atomic_write_json, atomic_write_text, read_json, sha256_file, sha256_obj


FORMAT_VERSION = 1


def _write_compact_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n",
    )


def _version_tag(version: int) -> str:
    return f"v{version:010d}"


def _artifact_file_name(stage: str, version: int) -> str:
    return f"{stage}-{_version_tag(version)}.json"


def _manifest_file(namespace: str, version: int) -> str:
    return f"{namespace}-{_version_tag(version)}.json"


def clear_snapshot_dir(snapshot_dir: str | Path) -> None:
    path = Path(snapshot_dir)
    if path in {Path("/"), Path("."), Path("")}:
        raise ValueError(f"refusing to clear unsafe snapshot path: {path}")
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def _remove_stale_temporaries(snapshot_dir: Path) -> None:
    for child in snapshot_dir.glob(".*.tmp"):
        try:
            child.unlink()
        except FileNotFoundError:
            pass


def save_snapshot(state: RuntimeState, snapshot_dir: str | Path) -> dict[str, Any]:
    """Write stage files, then manifest. Orphaned files are never referenced."""

    directory = Path(snapshot_dir)
    directory.mkdir(parents=True, exist_ok=True)
    _remove_stale_temporaries(directory)
    version_tag = _version_tag(state.version)

    stage_manifest: dict[str, Any] = {}
    for stage in STAGES:
        entries = {
            sample_id: artifacts[stage]
            for sample_id, artifacts in sorted(state.artifacts.items())
            if stage in artifacts
        }
        payload = {
            "format_version": FORMAT_VERSION,
            "stage": stage,
            "global_version": state.version,
            "stage_version": int(state.stage_versions[stage]),
            "entries": entries,
        }
        filename = _artifact_file_name(stage, state.version)
        path = directory / filename
        _write_compact_json(path, payload)
        stage_manifest[stage] = {
            "file": filename,
            "sha256": sha256_file(path),
            "stage_version": int(state.stage_versions[stage]),
        }

    failures_payload = {
        "format_version": FORMAT_VERSION,
        "global_version": state.version,
        "failures": [
            failure.to_dict()
            for failure in sorted(state.failures, key=lambda item: item.sort_key())
        ],
    }
    failures_filename = _manifest_file("failures", state.version)
    failures_path = directory / failures_filename
    _write_compact_json(failures_path, failures_payload)

    runtime_payload = {
        "format_version": FORMAT_VERSION,
        "global_version": state.version,
        "command_count": state.command_count,
        "effective_config": state.config,
        "effective_rules": state.rules,
        "base_config_hash": state.base_config_hash,
        "base_resource_fingerprint": state.base_resource_fingerprint,
        "samples_fingerprint": state.samples_fingerprint,
        "last_audit": state.last_audit,
    }
    runtime_filename = _manifest_file("runtime", state.version)
    runtime_path = directory / runtime_filename
    _write_compact_json(runtime_path, runtime_payload)

    manifest = {
        "format_version": FORMAT_VERSION,
        "global_version": state.version,
        "version_tag": version_tag,
        "command_count": state.command_count,
        "config_hash": state.config_hash,
        "resource_fingerprint": state.resource_fingerprint,
        "base_config_hash": state.base_config_hash,
        "base_resource_fingerprint": state.base_resource_fingerprint,
        "samples_fingerprint": state.samples_fingerprint,
        "completed_sample_ids": state.completed_sample_ids(),
        "stage_versions": {stage: int(state.stage_versions[stage]) for stage in STAGES},
        "stage_files": stage_manifest,
        "runtime_file": {
            "file": runtime_filename,
            "sha256": sha256_file(runtime_path),
        },
        "failures_file": {
            "file": failures_filename,
            "sha256": sha256_file(failures_path),
        },
    }
    atomic_write_json(directory / "manifest.json", manifest)
    return manifest


def load_manifest(snapshot_dir: str | Path) -> dict[str, Any] | None:
    path = Path(snapshot_dir) / "manifest.json"
    if not path.exists():
        return None
    try:
        manifest = read_json(path)
    except Exception as exc:
        raise SnapshotError(E_SNAPSHOT_INVALID, f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise SnapshotError(E_SNAPSHOT_INVALID, "unsupported snapshot manifest format")
    return manifest


def _verify_file_entry(directory: Path, entry: Any, expected_kind: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise SnapshotError(E_SNAPSHOT_INVALID, f"invalid {expected_kind} file entry")
    filename = entry.get("file")
    expected_hash = entry.get("sha256")
    if not isinstance(filename, str) or Path(filename).name != filename:
        raise SnapshotError(E_SNAPSHOT_INVALID, f"unsafe or invalid {expected_kind} filename")
    path = directory / filename
    if not path.exists():
        raise SnapshotError(E_SNAPSHOT_INVALID, f"missing {expected_kind} file: {filename}")
    actual_hash = sha256_file(path)
    if actual_hash != expected_hash:
        raise SnapshotError(
            E_SNAPSHOT_INVALID,
            f"{expected_kind} file hash mismatch: {filename}",
        )
    try:
        payload = read_json(path)
    except Exception as exc:
        raise SnapshotError(
            E_SNAPSHOT_INVALID, f"{expected_kind} file is invalid JSON: {filename}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise SnapshotError(E_SNAPSHOT_INVALID, f"{expected_kind} file must contain an object")
    return payload


def restore_snapshot(
    snapshot_dir: str | Path,
    *,
    base_config_hash: str,
    base_resource_fingerprint: str,
    samples_fingerprint: str,
    samples: list[Any],
) -> tuple[RuntimeState, dict[str, Any]] | None:
    directory = Path(snapshot_dir)
    manifest = load_manifest(directory)
    if manifest is None:
        return None

    if manifest.get("base_config_hash") != base_config_hash:
        raise SnapshotError(
            E_CONFIG_MISMATCH,
            "base configuration hash differs from the snapshot; refusing resume",
        )
    if manifest.get("base_resource_fingerprint") != base_resource_fingerprint:
        raise SnapshotError(
            E_CONFIG_MISMATCH,
            "base rule-pack fingerprint differs from the snapshot; refusing resume",
        )
    if manifest.get("samples_fingerprint") != samples_fingerprint:
        raise SnapshotError(
            E_CONFIG_MISMATCH,
            "sample content fingerprint differs from the snapshot; refusing resume",
        )

    runtime = _verify_file_entry(directory, manifest.get("runtime_file"), "runtime")
    failures_payload = _verify_file_entry(directory, manifest.get("failures_file"), "failures")
    global_version = int(manifest.get("global_version"))
    if runtime.get("global_version") != global_version or failures_payload.get("global_version") != global_version:
        raise SnapshotError(E_SNAPSHOT_INVALID, "runtime/failure version does not match manifest")

    effective_config = runtime.get("effective_config")
    effective_rules = runtime.get("effective_rules")
    if sha256_obj(effective_config) != manifest.get("config_hash"):
        raise SnapshotError(E_SNAPSHOT_INVALID, "effective config hash mismatch")
    if sha256_obj(effective_rules) != manifest.get("resource_fingerprint"):
        raise SnapshotError(E_SNAPSHOT_INVALID, "effective resource fingerprint mismatch")
    if runtime.get("base_config_hash") != base_config_hash:
        raise SnapshotError(E_CONFIG_MISMATCH, "runtime base config hash mismatch")
    if runtime.get("base_resource_fingerprint") != base_resource_fingerprint:
        raise SnapshotError(E_CONFIG_MISMATCH, "runtime base rule fingerprint mismatch")
    if runtime.get("samples_fingerprint") != samples_fingerprint:
        raise SnapshotError(E_CONFIG_MISMATCH, "runtime sample fingerprint mismatch")

    stage_files = manifest.get("stage_files")
    if not isinstance(stage_files, dict) or set(stage_files) != set(STAGES):
        raise SnapshotError(E_SNAPSHOT_INVALID, "manifest must list exactly four stage files")
    artifacts: dict[str, dict[str, dict[str, Any]]] = {}
    stage_versions: dict[str, int] = {}
    for stage in STAGES:
        payload = _verify_file_entry(directory, stage_files[stage], f"stage:{stage}")
        if payload.get("stage") != stage or payload.get("global_version") != global_version:
            raise SnapshotError(E_SNAPSHOT_INVALID, f"stage file identity mismatch: {stage}")
        expected_stage_version = int(stage_files[stage].get("stage_version"))
        if payload.get("stage_version") != expected_stage_version:
            raise SnapshotError(E_SNAPSHOT_INVALID, f"stage version mismatch: {stage}")
        stage_versions[stage] = expected_stage_version
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            raise SnapshotError(E_SNAPSHOT_INVALID, f"stage entries must be an object: {stage}")
        for sample_id, artifact in entries.items():
            if not isinstance(sample_id, str) or not isinstance(artifact, dict):
                raise SnapshotError(E_SNAPSHOT_INVALID, f"invalid stage entry: {stage}")
            artifacts.setdefault(sample_id, {})[stage] = artifact

    completed_ids = manifest.get("completed_sample_ids")
    if not isinstance(completed_ids, list) or not all(isinstance(item, str) for item in completed_ids):
        raise SnapshotError(E_SNAPSHOT_INVALID, "completed_sample_ids must be an array of strings")
    if completed_ids != sorted(set(completed_ids)):
        raise SnapshotError(E_SNAPSHOT_INVALID, "completed_sample_ids must be sorted and unique")
    actually_completed = sorted(
        sample_id
        for sample_id, stages in artifacts.items()
        if all(stage in stages for stage in STAGES)
    )
    if completed_ids != actually_completed:
        raise SnapshotError(E_SNAPSHOT_INVALID, "completed sample list does not match stage files")

    failures: list[Failure] = []
    for raw in failures_payload.get("failures", []):
        if not isinstance(raw, dict):
            raise SnapshotError(E_SNAPSHOT_INVALID, "failure entry must be an object")
        try:
            failures.append(Failure(**raw))
        except TypeError as exc:
            raise SnapshotError(E_SNAPSHOT_INVALID, f"invalid failure entry: {exc}") from exc
    failures.sort(key=lambda item: item.sort_key())

    sample_ids = {sample.sample_id for sample in samples}
    unknown_artifact_ids = set(artifacts) - sample_ids
    if unknown_artifact_ids:
        raise SnapshotError(
            E_CONFIG_MISMATCH,
            f"snapshot references unknown sample ids: {sorted(unknown_artifact_ids)[:5]}",
        )
    state = RuntimeState(
        version=global_version,
        command_count=int(manifest.get("command_count", runtime.get("command_count", 0))),
        samples=sorted(samples, key=lambda item: item.sample_id),
        rules=effective_rules,
        config=effective_config,
        artifacts=artifacts,
        failures=failures,
        base_config_hash=base_config_hash,
        base_resource_fingerprint=base_resource_fingerprint,
        samples_fingerprint=samples_fingerprint,
        stage_versions=stage_versions,
        last_audit=runtime.get("last_audit", {}),
    )
    info = {
        "recovered": True,
        "recovery_point": {
            "global_version": global_version,
            "stage_versions": stage_versions,
            "completed_sample_count": len(completed_ids),
            "config_hash": manifest.get("config_hash"),
            "resource_fingerprint": manifest.get("resource_fingerprint"),
        },
        "manifest": manifest,
    }
    return state, info


def prune_old_snapshots(snapshot_dir: str | Path, keep_versions: int = 3) -> None:
    """Best-effort cleanup; manifest is never modified by this function."""

    directory = Path(snapshot_dir)
    manifests = sorted(
        directory.glob("runtime-v*.json"),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    keep = {path.name for path in manifests[:keep_versions]}
    keep.add("manifest.json")
    for manifest_path in manifests:
        if manifest_path.name in keep:
            continue
        try:
            payload = read_json(manifest_path)
        except Exception:
            continue
        version_tag = _version_tag(int(payload.get("global_version", -1)))
        for path in directory.iterdir():
            if path.name.endswith(f"-{version_tag}.json") and path.name != manifest_path.name:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
