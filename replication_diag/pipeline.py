"""Source loading, dry-run validation, and digesting."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .errors import DiagnosticsError
from .log_parser import parse_logs
from .merge import merge_records
from .models import LogRecord, PositionRecord, SourceBundle
from .util import sha256_bytes, stable_json_bytes
from .v1_parser import parse_v1
from .v2_parser import parse_v2


def _semantic_record(record: PositionRecord) -> dict[str, Any]:
    payload = record.to_json()
    payload.pop("location", None)
    return payload


def _semantic_log(record: LogRecord) -> dict[str, Any]:
    payload = record.to_json()
    payload.pop("location", None)
    return payload


def source_digest(
    v1_digest: str,
    v2_digest: str,
    logs_digest: str,
    config: Config,
    manifest: dict[str, Any] | None,
) -> str:
    payload = {
        "v1": v1_digest,
        "v2": v2_digest,
        "logs": logs_digest,
        "config": config.as_dict(),
        "manifest": manifest,
    }
    return sha256_bytes(stable_json_bytes(payload))


def validate_logs(merged: list[PositionRecord], logs: list[LogRecord]) -> None:
    by_tx = {record.tx_id: record for record in merged}
    for log in logs:
        record = by_tx.get(log.tx_id)
        if record is None:
            raise DiagnosticsError(
                "E_LOG_ORPHAN",
                location=log.location,
                details={"tx_id": log.tx_id},
            )
        if not record.committed:
            raise DiagnosticsError(
                "E_LOG_ORPHAN",
                message="replay stream includes an uncommitted position",
                location=log.location,
                details={"tx_id": log.tx_id},
            )
        if record.payload_bytes != log.payload_bytes:
            raise DiagnosticsError(
                "E_LOG_BYTES",
                location=log.location,
                details={
                    "tx_id": log.tx_id,
                    "position_payload_bytes": record.payload_bytes,
                    "log_payload_bytes": log.payload_bytes,
                },
            )


def load_sources(config: Config, *, include_v2: bool = True) -> SourceBundle:
    v1_records = parse_v1(config.v1_path, config)
    v1_digest = sha256_bytes(
        stable_json_bytes([_semantic_record(record) for record in v1_records])
    )

    v2_records: list[PositionRecord] = []
    manifest: dict[str, Any] | None = None
    v2_digest = ""
    if include_v2 and config.v2_manifest is not None:
        v2_records, manifest = parse_v2(config.v2_manifest, config)
        v2_digest = sha256_bytes(
            stable_json_bytes([_semantic_record(record) for record in v2_records])
        )

    logs, logs_digest = parse_logs(config.logs_path)
    merged = merge_records(v1_records, v2_records, config)
    validate_logs(merged, logs)
    return SourceBundle(
        v1_records=v1_records,
        v2_records=v2_records,
        log_records=logs,
        manifest=manifest,
        v1_digest=v1_digest,
        v2_digest=v2_digest,
        logs_digest=logs_digest,
    )


def bundle_digest(bundle: SourceBundle, config: Config) -> str:
    return source_digest(
        bundle.v1_digest,
        bundle.v2_digest,
        bundle.logs_digest,
        config,
        bundle.manifest,
    )


def package_paths(manifest_path: Path, config: Config) -> tuple[list[PositionRecord], dict[str, Any]]:
    return parse_v2(manifest_path, config)
