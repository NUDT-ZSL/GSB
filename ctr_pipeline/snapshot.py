"""Atomic stage snapshots, manifest management, and resume helpers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import E_CONFIG_MISMATCH, E_STATE_INVALID, FailureRecord, PipelineError
from .io_utils import content_digest, read_json, write_json_atomic


STAGES = ("normalize", "quantize", "fusion", "truncate")
SCHEMA_VERSION = 1


def _safe_request_dir(request_id: str) -> str:
    return hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:20]


class SnapshotStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir).resolve()
        self.requests_dir = self.state_dir / "requests"
        self.manifest_path = self.state_dir / "manifest.json"

    def ensure_writable(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        probe = self.state_dir / f".write-probe-{os.getpid()}.tmp"
        try:
            with probe.open("w", encoding="utf-8") as handle:
                handle.write("ok")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            probe.unlink(missing_ok=True)

    def load_manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.exists():
            return None
        try:
            data = read_json(self.manifest_path)
        except Exception as exc:
            raise PipelineError(E_STATE_INVALID, "snapshot", f"cannot read manifest: {exc}") from exc
        if not isinstance(data, dict):
            raise PipelineError(E_STATE_INVALID, "snapshot", "manifest root must be an object")
        required = {
            "schema_version",
            "run_id",
            "config_hash",
            "params_hash",
            "candidates_hash",
            "global_version",
            "cursor_index",
            "processed_request_ids",
            "request_records",
            "failure_list",
            "budget_curve",
            "injection_hits",
            "stage_versions",
        }
        missing = sorted(required - set(data))
        if missing:
            raise PipelineError(
                E_STATE_INVALID, "snapshot", f"manifest is missing fields: {missing}"
            )
        if data.get("schema_version") != SCHEMA_VERSION:
            raise PipelineError(
                E_CONFIG_MISMATCH,
                "snapshot",
                (
                    f"manifest schema_version={data.get('schema_version')!r} does not match "
                    f"supported version {SCHEMA_VERSION}"
                ),
            )
        return data

    def new_manifest(
        self,
        *,
        run_id: str,
        config_hash: str,
        params_hash: str,
        candidates_hash: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "config_hash": config_hash,
            "params_hash": params_hash,
            "candidates_hash": candidates_hash,
            "global_version": 0,
            "cursor_index": 0,
            "processed_request_ids": [],
            "request_records": {},
            "failure_list": [],
            "budget_curve": [],
            "injection_hits": {},
            "stage_versions": {},
        }

    def request_dir(self, request_id: str) -> Path:
        path = self.requests_dir / _safe_request_dir(request_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write_stage(self, request_id: str, stage: str, payload: dict[str, Any]) -> str:
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage}")
        path = self.request_dir(request_id) / f"{stage}.json"
        write_json_atomic(path, payload)
        return content_digest(payload)

    def commit_success(
        self,
        manifest: dict[str, Any],
        *,
        request_id: str,
        artifacts: dict[str, dict[str, Any]],
        request_report: dict[str, Any],
        budget_events: Iterable[dict[str, Any]],
        injection_hits: Iterable[str],
    ) -> None:
        if request_id in manifest["request_records"]:
            raise PipelineError(
                E_STATE_INVALID, "snapshot", f"request {request_id!r} was already committed"
            )
        versions: dict[str, str] = {}
        artifact_versions: dict[str, str] = {}
        for stage in STAGES:
            if stage not in artifacts:
                raise PipelineError(
                    E_STATE_INVALID, "snapshot", f"missing {stage} artifact for {request_id!r}"
                )
            versions[stage] = self.write_stage(request_id, stage, artifacts[stage])
            artifact_versions[stage] = versions[stage]
        rel_dir = str(self.request_dir(request_id).relative_to(self.state_dir)).replace(os.sep, "/")
        manifest["request_records"][request_id] = {
            "dir": rel_dir,
            "status": "success",
            "artifact_versions": artifact_versions,
        }
        self._advance_manifest(
            manifest,
            request_id=request_id,
            budget_events=budget_events,
            injection_hits=injection_hits,
        )
        self._recompute_stage_versions(manifest)

    def commit_failure(
        self,
        manifest: dict[str, Any],
        *,
        failure: FailureRecord,
        budget_events: Iterable[dict[str, Any]],
        injection_hits: Iterable[str],
    ) -> None:
        request_id = failure.request_id
        if request_id in manifest["request_records"]:
            raise PipelineError(
                E_STATE_INVALID, "snapshot", f"request {request_id!r} was already committed"
            )
        directory = self.request_dir(request_id)
        payload = {"schema_version": SCHEMA_VERSION, "failure": failure.to_dict()}
        failure_version = content_digest(payload)
        write_json_atomic(directory / "failure.json", payload)
        manifest["request_records"][request_id] = {
            "dir": str(directory.relative_to(self.state_dir)).replace(os.sep, "/"),
            "status": "failure",
            "artifact_versions": {"failure": failure_version},
        }
        manifest["failure_list"].append(failure.to_dict())
        manifest["failure_list"].sort(
            key=lambda item: (
                item["request_id"],
                item["stage"],
                item["code"],
                item.get("candidate_id") or "",
                tuple(item.get("injection_ids", [])),
                item["exception_type"],
                item["message"],
            )
        )
        self._advance_manifest(
            manifest,
            request_id=request_id,
            budget_events=budget_events,
            injection_hits=injection_hits,
        )
        self._recompute_stage_versions(manifest)

    def _advance_manifest(
        self,
        manifest: dict[str, Any],
        *,
        request_id: str,
        budget_events: Iterable[dict[str, Any]],
        injection_hits: Iterable[str],
    ) -> None:
        manifest["global_version"] = int(manifest["global_version"]) + 1
        manifest["processed_request_ids"].append(request_id)
        manifest["cursor_index"] = len(manifest["processed_request_ids"])
        for event in budget_events:
            expected_sequence = len(manifest["budget_curve"]) + 1
            if int(event["sequence"]) != expected_sequence:
                raise PipelineError(
                    E_STATE_INVALID,
                    "snapshot",
                    f"budget event sequence {event['sequence']} does not match {expected_sequence}",
                )
            manifest["budget_curve"].append(dict(event))
        for injection_id in sorted(set(injection_hits)):
            manifest["injection_hits"][injection_id] = {
                "hit": True,
                "request_id": request_id,
            }

    def _recompute_stage_versions(self, manifest: dict[str, Any]) -> None:
        for stage in STAGES:
            items: list[tuple[str, str]] = []
            for request_id, record in manifest["request_records"].items():
                version = record.get("artifact_versions", {}).get(stage)
                if version:
                    items.append((request_id, version))
            items.sort()
            manifest["stage_versions"][stage] = {
                "version": content_digest(items) if items else content_digest([]),
                "request_count": len(items),
            }

    def save_manifest(self, manifest: dict[str, Any]) -> None:
        # Keep ordering deterministic in the serialized file.
        manifest["processed_request_ids"] = list(manifest["processed_request_ids"])
        manifest["failure_list"] = sorted(
            manifest["failure_list"],
            key=lambda item: (
                item["request_id"],
                item["stage"],
                item["code"],
                item.get("candidate_id") or "",
                tuple(item.get("injection_ids", [])),
                item["exception_type"],
                item["message"],
            ),
        )
        manifest["injection_hits"] = {
            key: manifest["injection_hits"][key] for key in sorted(manifest["injection_hits"])
        }
        write_json_atomic(self.manifest_path, manifest)

    def load_request_report(self, manifest: dict[str, Any], request_id: str) -> dict[str, Any] | None:
        record = manifest["request_records"].get(request_id)
        if not record or record.get("status") != "success":
            return None
        path = self.state_dir / record["dir"] / "truncate.json"
        try:
            payload = read_json(path)
        except Exception as exc:
            raise PipelineError(
                E_STATE_INVALID,
                "snapshot",
                f"cannot read truncate artifact for {request_id!r}: {exc}",
            ) from exc
        report = payload.get("report")
        if not isinstance(report, dict):
            raise PipelineError(
                E_STATE_INVALID,
                "snapshot",
                f"truncate artifact for {request_id!r} has no request report",
            )
        return report

    def load_failure(self, manifest: dict[str, Any], request_id: str) -> FailureRecord | None:
        record = manifest["request_records"].get(request_id)
        if not record or record.get("status") != "failure":
            return None
        path = self.state_dir / record["dir"] / "failure.json"
        try:
            payload = read_json(path)
        except Exception as exc:
            raise PipelineError(
                E_STATE_INVALID,
                "snapshot",
                f"cannot read failure artifact for {request_id!r}: {exc}",
            ) from exc
        return FailureRecord.from_dict(payload["failure"])

    def artifact_versions_for_state(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {
            request_id: record.get("artifact_versions", {})
            for request_id, record in sorted(manifest["request_records"].items())
        }


def manifest_determinism_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "config_hash": manifest["config_hash"],
        "params_hash": manifest["params_hash"],
        "candidates_hash": manifest["candidates_hash"],
        "global_version": manifest["global_version"],
        "processed_request_ids": manifest["processed_request_ids"],
        "failure_list": manifest["failure_list"],
        "budget_curve": manifest["budget_curve"],
        "injection_hits": manifest["injection_hits"],
        "stage_versions": manifest["stage_versions"],
        "artifact_versions": manifest.get("artifact_versions", {}),
    }


def manifest_digest(manifest: dict[str, Any]) -> str:
    return content_digest(manifest_determinism_payload(manifest))
