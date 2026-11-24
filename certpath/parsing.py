"""Strict parsing and normalization for all local sample declarations."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .canon import load_json, sha256_file, strict_json_loads
from .errors import (
    E_BOOL,
    E_CHAIN_FORMAT,
    E_CHAIN_LIMIT,
    E_DUP_ANCHOR_ID,
    E_DUP_CERT_ID,
    E_DUP_CHAIN_ID,
    E_DUP_ROOT_REF,
    E_FINGERPRINT,
    E_GENERATION,
    E_ID,
    E_ISSUER_UNRESOLVED,
    E_PARSE,
    E_PATH_LENGTH,
    E_ROOT_REF_NOT_ROOT,
    E_ROOT_REF_UNRESOLVED,
    E_SCHEMA,
    E_SHARD_COUNT,
    E_SHARD_DIGEST,
    E_TIME_TYPE,
    E_TIME_WINDOW,
    E_V1_CLOCK,
    E_V1_CONSTRAINT,
    E_V2_META,
    CertPathError,
    Rejection,
    reject,
)
from .model import (
    Certificate,
    ChainDeclaration,
    V1AnchorRecord,
    V1Snapshot,
    V2AnchorRecord,
    V2Snapshot,
)


_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


def normalize_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError("expected sha256:<64 hex characters>")
    return value.lower()


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_int(
    value: Any,
    code: str,
    field_name: str,
    *,
    minimum: int | None = None,
) -> int:
    if not _is_int(value):
        raise CertPathError(code, f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise CertPathError(code, f"{field_name} must be >= {minimum}")
    return value


def _require_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise CertPathError(E_BOOL, f"{field_name} must be a boolean")
    return value


def _require_id(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CertPathError(E_ID, f"{field_name} must be a non-empty trimmed string")
    return value


def _load_json_file(path: Path, file_id: str) -> Any:
    try:
        return load_json(path)
    except Exception as exc:
        raise CertPathError(
            E_PARSE,
            f"cannot parse JSON: {exc}",
            details={"file_id": file_id},
        ) from exc


def parse_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    data = _load_json_file(source, source.name)
    if not isinstance(data, dict):
        raise CertPathError(E_SCHEMA, "manifest must be a JSON object")
    if data.get("format") != "certpath.sample":
        raise CertPathError(E_SCHEMA, "manifest format must be certpath.sample")
    clock = _require_int(
        data.get("observation_clock_ms"),
        E_TIME_TYPE,
        "observation_clock_ms",
        minimum=0,
    )
    files = data.get("files")
    if not isinstance(files, dict):
        raise CertPathError(E_SCHEMA, "manifest files must be an object")
    for key in ("certificates", "chains", "v1", "v2"):
        if not isinstance(files.get(key), str) or not files[key]:
            raise CertPathError(E_SCHEMA, f"manifest files.{key} is required")
    result = dict(data)
    result["observation_clock_ms"] = clock
    return result


def _extract_records(data: Any, list_key: str, file_id: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(list_key), list):
        return data[list_key]
    raise CertPathError(
        E_SCHEMA,
        f"{file_id} must be a JSON array or contain a {list_key} array",
        details={"file_id": file_id},
    )


def parse_certificates(
    path: str | Path,
    *,
    file_id: str | None = None,
) -> tuple[list[Certificate], list[Rejection]]:
    source = Path(path)
    fid = file_id or source.name
    records = _extract_records(_load_json_file(source, fid), "certificates", fid)
    accepted: list[Certificate] = []
    rejected: list[Rejection] = []
    seen_ids: set[str] = set()

    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict):
                raise CertPathError(E_SCHEMA, "certificate record must be an object")
            certificate_id = _require_id(
                record.get("certificate_id"), "certificate_id"
            )
            if certificate_id in seen_ids:
                raise CertPathError(
                    E_DUP_CERT_ID,
                    f"duplicate certificate_id: {certificate_id}",
                )
            subject_id = _require_id(record.get("subject_id"), "subject_id")
            issuer_id = _require_id(record.get("issuer_id"), "issuer_id")
            try:
                public_fingerprint = normalize_fingerprint(
                    record.get("public_key_fingerprint")
                )
            except ValueError as exc:
                raise CertPathError(
                    E_FINGERPRINT, f"public_key_fingerprint: {exc}"
                ) from exc
            try:
                signature_digest = normalize_fingerprint(
                    record.get("signature_digest")
                )
            except ValueError as exc:
                raise CertPathError(
                    E_FINGERPRINT, f"signature_digest: {exc}"
                ) from exc
            not_before = _require_int(
                record.get("not_before_ms"), E_TIME_TYPE, "not_before_ms"
            )
            not_after = _require_int(
                record.get("not_after_ms"), E_TIME_TYPE, "not_after_ms"
            )
            if not_before >= not_after:
                raise CertPathError(
                    E_TIME_WINDOW,
                    "not_before_ms must be less than not_after_ms",
                )
            cross_signed = _require_bool(
                record.get("cross_signed"), "cross_signed"
            )
            revoked = _require_bool(record.get("revoked"), "revoked")
            seen_ids.add(certificate_id)
            accepted.append(
                Certificate(
                    certificate_id=certificate_id,
                    subject_id=subject_id,
                    issuer_id=issuer_id,
                    public_key_fingerprint=public_fingerprint,
                    signature_digest=signature_digest,
                    not_before_ms=not_before,
                    not_after_ms=not_after,
                    cross_signed=cross_signed,
                    revoked=revoked,
                    record_index=index,
                )
            )
        except CertPathError as exc:
            rejected.append(reject(fid, index, exc.code, exc.message))

    subjects = {item.subject_id for item in accepted}
    resolvable: list[Certificate] = []
    for cert in accepted:
        if cert.issuer_id not in subjects:
            rejected.append(
                reject(
                    fid,
                    cert.record_index,
                    E_ISSUER_UNRESOLVED,
                    f"issuer_id {cert.issuer_id!r} has no certificate subject",
                    "issuer_id",
                )
            )
        else:
            resolvable.append(cert)

    rejected.sort(key=lambda item: (item.record_index, item.code))
    return resolvable, rejected


def parse_chains(
    path: str | Path,
    *,
    file_id: str | None = None,
) -> tuple[list[ChainDeclaration], list[Rejection]]:
    source = Path(path)
    fid = file_id or source.name
    records = _extract_records(_load_json_file(source, fid), "chains", fid)
    accepted: list[ChainDeclaration] = []
    rejected: list[Rejection] = []
    seen_ids: set[str] = set()

    for index, record in enumerate(records):
        try:
            if not isinstance(record, dict):
                raise CertPathError(E_SCHEMA, "chain record must be an object")
            chain_id = _require_id(record.get("chain_id"), "chain_id")
            if chain_id in seen_ids:
                raise CertPathError(
                    E_DUP_CHAIN_ID, f"duplicate chain_id: {chain_id}"
                )
            raw_ids = record.get("certificate_ids")
            if not isinstance(raw_ids, list) or len(raw_ids) < 2:
                raise CertPathError(
                    E_CHAIN_FORMAT,
                    "certificate_ids must be an array with at least two IDs",
                )
            cert_ids = tuple(
                _require_id(item, f"certificate_ids[{pos}]")
                for pos, item in enumerate(raw_ids)
            )
            if len(set(cert_ids)) != len(cert_ids):
                raise CertPathError(
                    E_CHAIN_FORMAT, "certificate_ids must not contain duplicates"
                )
            limit = _require_int(
                record.get("enumeration_limit"),
                E_CHAIN_LIMIT,
                "enumeration_limit",
                minimum=1,
            )
            seen_ids.add(chain_id)
            accepted.append(
                ChainDeclaration(
                    chain_id=chain_id,
                    certificate_ids=cert_ids,
                    enumeration_limit=limit,
                    record_index=index,
                )
            )
        except CertPathError as exc:
            rejected.append(reject(fid, index, exc.code, exc.message))

    rejected.sort(key=lambda item: (item.record_index, item.code))
    return accepted, rejected


def parse_v1_snapshot(
    path: str | Path,
    certificates: list[Certificate],
    observation_clock_ms: int,
    *,
    file_id: str | None = None,
) -> tuple[V1Snapshot | None, list[Rejection]]:
    source = Path(path)
    fid = file_id or source.name
    data = _load_json_file(source, fid)
    if not isinstance(data, dict):
        raise CertPathError(E_SCHEMA, "v1 snapshot must be an object")
    if data.get("format") != "certpath.v1":
        raise CertPathError(E_SCHEMA, "v1 format must be certpath.v1")
    seconds = _require_int(
        data.get("relative_effective_seconds"),
        E_V1_CLOCK,
        "relative_effective_seconds",
        minimum=0,
    )
    no_constraint = data.get("no_path_length_constraint")
    if no_constraint is not True:
        raise CertPathError(
            E_V1_CONSTRAINT,
            "no_path_length_constraint must be true for v1",
        )
    trusted = data.get("trusted_root_ids", [])
    revoked = data.get("revoked_root_ids", [])
    if not isinstance(trusted, list):
        raise CertPathError(E_SCHEMA, "trusted_root_ids must be a string array")
    if not isinstance(revoked, list):
        raise CertPathError(E_SCHEMA, "revoked_root_ids must be a string array")

    cert_by_id = {item.certificate_id: item for item in certificates}
    rejected: list[Rejection] = []
    anchor_by_id: dict[str, V1AnchorRecord] = {}
    accepted_trusted: list[str] = []
    accepted_revoked: list[str] = []
    base_ms = observation_clock_ms + seconds * 1000
    quantization = {
        "basis": "observation_clock_ms + relative_effective_seconds * 1000",
        "relative_effective_seconds": seconds,
        "observation_clock_ms": observation_clock_ms,
        "lower_ms": base_ms,
        "upper_ms": base_ms + 999,
        "max_error_ms": 999,
        "endpoint_rule": (
            "relative_effective_seconds is read as the lower endpoint of a "
            "floor-quantized one-second interval"
        ),
    }

    trusted_set: set[str] = set()
    revoked_set: set[str] = set()

    def unresolved_reason(root_id: str, index: int) -> bool:
        cert = cert_by_id.get(root_id)
        if cert is None:
            rejected.append(
                reject(
                    fid,
                    index,
                    E_ROOT_REF_UNRESOLVED,
                    f"trusted root ID {root_id!r} cannot be resolved",
                )
            )
            return True
        if cert.subject_id != cert.issuer_id:
            rejected.append(
                reject(
                    fid,
                    index,
                    E_ROOT_REF_NOT_ROOT,
                    f"certificate {root_id!r} is not self-issued",
                )
            )
            return True
        anchor_by_id[root_id] = V1AnchorRecord(
            certificate_id=root_id,
            fingerprint=cert.public_key_fingerprint,
            subject_id=cert.subject_id,
            relative_effective_seconds=seconds,
            effective_ms=base_ms,
            effective_low_ms=base_ms,
            effective_high_ms=base_ms + 999,
            trusted=root_id in trusted_set and root_id not in revoked_set,
            revoked=root_id in revoked_set,
        )
        return False

    seen_trusted: set[str] = set()
    for index, raw_root_id in enumerate(trusted):
        try:
            root_id = _require_id(raw_root_id, f"trusted_root_ids[{index}]")
        except CertPathError as exc:
            rejected.append(reject(fid, index, exc.code, exc.message))
            continue
        if root_id in seen_trusted:
            rejected.append(
                reject(
                    fid,
                    index,
                    E_DUP_ROOT_REF,
                    f"duplicate trusted root reference: {root_id}",
                )
            )
            continue
        seen_trusted.add(root_id)
        trusted_set.add(root_id)
        accepted_trusted.append(root_id)
        unresolved_reason(root_id, index)
    seen_revoked: set[str] = set()
    for index, raw_root_id in enumerate(revoked):
        combined_index = len(trusted) + index
        try:
            root_id = _require_id(raw_root_id, f"revoked_root_ids[{index}]")
        except CertPathError as exc:
            rejected.append(
                reject(fid, combined_index, exc.code, exc.message)
            )
            continue
        if root_id in seen_revoked:
            rejected.append(
                reject(
                    fid,
                    combined_index,
                    E_DUP_ROOT_REF,
                    f"duplicate revoked root reference: {root_id}",
                )
            )
            continue
        seen_revoked.add(root_id)
        revoked_set.add(root_id)
        accepted_revoked.append(root_id)
        if root_id not in anchor_by_id:
            unresolved_reason(root_id, combined_index)
    for root_id, anchor in list(anchor_by_id.items()):
        if anchor.revoked:
            anchor_by_id[root_id] = V1AnchorRecord(
                certificate_id=anchor.certificate_id,
                fingerprint=anchor.fingerprint,
                subject_id=anchor.subject_id,
                relative_effective_seconds=anchor.relative_effective_seconds,
                effective_ms=anchor.effective_ms,
                effective_low_ms=anchor.effective_low_ms,
                effective_high_ms=anchor.effective_high_ms,
                trusted=False,
                revoked=True,
            )

    snapshot = V1Snapshot(
        relative_effective_seconds=seconds,
        trusted_root_ids=tuple(accepted_trusted),
        revoked_root_ids=tuple(accepted_revoked),
        no_path_length_constraint=True,
        anchors=tuple(
            anchor_by_id[key] for key in sorted(anchor_by_id)
        ),
        conversion=quantization,
    )
    rejected.sort(key=lambda item: (item.record_index, item.code))
    return snapshot, rejected


def _read_v2_metadata(
    db_path: Path,
    file_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise CertPathError(
            E_V2_META,
            f"cannot open v2 metadata database: {exc}",
            details={"file_id": file_id},
        ) from exc
    try:
        try:
            rows = connection.execute(
                "SELECT key, value FROM snapshot_meta ORDER BY key"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CertPathError(
                E_V2_META,
                f"snapshot_meta is invalid: {exc}",
                details={"file_id": file_id},
            ) from exc
        meta: dict[str, Any] = {}
        for key, value in rows:
            try:
                meta[str(key)] = json.loads(value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise CertPathError(
                    E_V2_META,
                    f"snapshot_meta value for {key!r} is not JSON: {exc}",
                    details={"file_id": file_id},
                ) from exc
        try:
            shard_rows = connection.execute(
                "SELECT shard_id, file, generation, effective_ms, record_count, digest "
                "FROM shards ORDER BY shard_id"
            ).fetchall()
        except sqlite3.Error as exc:
            raise CertPathError(
                E_V2_META,
                f"shards table is invalid: {exc}",
                details={"file_id": file_id},
            ) from exc
        shards = [
            {
                "shard_id": row[0],
                "file": row[1],
                "generation": row[2],
                "effective_ms": row[3],
                "record_count": row[4],
                "digest": row[5],
            }
            for row in shard_rows
        ]
        return meta, shards
    finally:
        connection.close()


def parse_v2_snapshot(
    path: str | Path,
    *,
    file_id: str | None = None,
) -> tuple[V2Snapshot | None, list[Rejection]]:
    v2_dir = Path(path)
    db_path = v2_dir / "meta.db"
    fid = file_id or f"{v2_dir.name}/meta.db"
    if not db_path.is_file():
        raise CertPathError(
            E_V2_META,
            "v2 metadata database meta.db does not exist",
            details={"file_id": fid},
        )
    meta, shards = _read_v2_metadata(db_path, fid)
    if meta.get("format") != "certpath.v2.meta" or meta.get("version") != 2:
        raise CertPathError(E_V2_META, "v2 metadata format/version is invalid")
    generation = _require_int(
        meta.get("generation"), E_GENERATION, "v2 generation", minimum=0
    )
    effective_ms = _require_int(
        meta.get("effective_ms"), E_TIME_TYPE, "v2 effective_ms", minimum=0
    )
    declared_count = _require_int(
        meta.get("shard_count"), E_SHARD_COUNT, "v2 shard_count", minimum=1
    )
    if declared_count != len(shards):
        raise CertPathError(
            E_SHARD_COUNT,
            "v2 shard_count does not match metadata rows",
            details={"declared": declared_count, "actual": len(shards)},
        )

    permitted_root = v2_dir.resolve()
    accepted: list[V2AnchorRecord] = []
    rejected: list[Rejection] = []
    seen_anchor_ids: set[str] = set()
    seen_shard_ids: set[str] = set()
    for shard_meta in shards:
        shard_id = _require_id(shard_meta.get("shard_id"), "shard_id")
        if shard_id in seen_shard_ids:
            raise CertPathError(E_V2_META, f"duplicate shard_id: {shard_id}")
        seen_shard_ids.add(shard_id)
        rel_file = shard_meta.get("file")
        if not isinstance(rel_file, str) or not rel_file:
            raise CertPathError(E_V2_META, f"shard {shard_id} has no file")
        shard_path = (v2_dir / rel_file).resolve()
        try:
            shard_path.relative_to(permitted_root)
        except ValueError as exc:
            raise CertPathError(
                E_V2_META, f"shard {shard_id} escapes the v2 directory"
            ) from exc
        if not shard_path.is_file():
            raise CertPathError(
                E_V2_META, f"shard file does not exist: {rel_file}"
            )
        expected_digest = shard_meta.get("digest")
        if not isinstance(expected_digest, str) or sha256_file(
            shard_path
        ) != expected_digest:
            raise CertPathError(
                E_SHARD_DIGEST,
                f"shard digest mismatch: {rel_file}",
                details={"shard_id": shard_id},
            )
        try:
            data = strict_json_loads(shard_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CertPathError(
                E_PARSE,
                f"cannot parse v2 shard {rel_file}: {exc}",
                details={"file_id": rel_file},
            ) from exc
        if not isinstance(data, dict) or data.get("format") != "certpath.v2.shard":
            raise CertPathError(E_V2_META, f"invalid shard format: {rel_file}")
        if data.get("shard_id") != shard_id:
            raise CertPathError(
                E_V2_META, f"shard_id mismatch in {rel_file}"
            )
        if data.get("generation") != shard_meta.get("generation"):
            raise CertPathError(
                E_V2_META, f"generation mismatch in {rel_file}"
            )
        if data.get("effective_ms") != shard_meta.get("effective_ms"):
            raise CertPathError(
                E_V2_META, f"effective_ms mismatch in {rel_file}"
            )
        records = data.get("records")
        if not isinstance(records, list):
            raise CertPathError(E_V2_META, f"records must be an array in {rel_file}")
        if len(records) != shard_meta.get("record_count"):
            raise CertPathError(
                E_SHARD_COUNT,
                f"record_count mismatch in {rel_file}",
                details={
                    "declared": shard_meta.get("record_count"),
                    "actual": len(records),
                },
            )
        record_file_id = f"{fid}#{rel_file}"
        for index, record in enumerate(records):
            try:
                if not isinstance(record, dict):
                    raise CertPathError(E_SCHEMA, "anchor record must be an object")
                anchor_id = _require_id(record.get("anchor_id"), "anchor_id")
                if anchor_id in seen_anchor_ids:
                    raise CertPathError(
                        E_DUP_ANCHOR_ID,
                        f"duplicate anchor_id: {anchor_id}",
                    )
                try:
                    fingerprint = normalize_fingerprint(
                        record.get("fingerprint")
                    )
                except ValueError as exc:
                    raise CertPathError(E_FINGERPRINT, f"fingerprint: {exc}") from exc
                subject_id = _require_id(record.get("subject_id"), "subject_id")
                record_effective = _require_int(
                    record.get("effective_ms"),
                    E_TIME_TYPE,
                    "effective_ms",
                    minimum=0,
                )
                record_generation = _require_int(
                    record.get("generation"),
                    E_GENERATION,
                    "generation",
                    minimum=0,
                )
                path_length = _require_int(
                    record.get("path_length"),
                    E_PATH_LENGTH,
                    "path_length",
                    minimum=0,
                )
                trusted = _require_bool(record.get("trusted"), "trusted")
                tombstone = _require_bool(record.get("tombstone"), "tombstone")
                revoked = _require_bool(record.get("revoked"), "revoked")
                seen_anchor_ids.add(anchor_id)
                accepted.append(
                    V2AnchorRecord(
                        anchor_id=anchor_id,
                        fingerprint=fingerprint,
                        subject_id=subject_id,
                        effective_ms=record_effective,
                        generation=record_generation,
                        path_length=path_length,
                        trusted=trusted,
                        tombstone=tombstone,
                        revoked=revoked,
                        shard_id=shard_id,
                        record_index=index,
                    )
                )
            except CertPathError as exc:
                rejected.append(
                    reject(record_file_id, index, exc.code, exc.message)
                )

    accepted.sort(
        key=lambda item: (
            item.fingerprint,
            item.generation,
            item.effective_ms,
            item.anchor_id,
        )
    )
    rejected.sort(key=lambda item: (item.file_id, item.record_index, item.code))
    snapshot = V2Snapshot(
        generation=generation,
        effective_ms=effective_ms,
        anchors=tuple(accepted),
        shard_count=len(shards),
        metadata=meta,
    )
    return snapshot, rejected
